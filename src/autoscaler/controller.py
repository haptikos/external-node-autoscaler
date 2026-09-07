"""The decision loop. Level-based: all state is derived fresh every pass.

  1. DEMAND  = units requested by Unschedulable pending pods, SPLIT BY POOL
  2. SUPPLY  = free units on that pool's Ready virtual nodes + its booting machines
  3. deficit > 0            -> mint the machine's CAs, store what we keep, rent
                               a machine that boots k3s + liqo from them
  4. machine reports ready  -> liqoctl peer -> ResourceSlice -> virtual node,
                               then label that node for its pool
  5. virtual node idle      -> unpeer, terminate, forget
  6. virtual node NotReady  -> same, once dead_s has passed
  7. boot/peer timeout      -> terminate (retry next pass if demand holds)
  8. orphans (peering with no machine, credentials with no machine) -> clean

Step 3 is where the trust starts: the operator generates each machine's
certificate authorities before that machine exists, so it can assemble a
kubeconfig for a cluster that has not booted yet. See pki.py.

Demand, supply and bounds are per pool: one total across pools lets free
capacity in a cheap CPU pool cancel a pod that asked for an A10, and nothing is
rented. Only the rules that dispose of machines stay fleet-wide, so a machine is
reapable even when its pool has left the config.
"""
import math
import os
import time
from dataclasses import dataclass, field, replace

from . import logging_setup, pki
from .labels import POOL_LABEL
from . import packing
from .pools import Pool, headroom
from . import resources
from .resources import Resources
from .liqo import PeeringBlocked, TenantTerminating
from .providers.base import Bootstrap

log = logging_setup.get("controller")

#: Machines whose pool is no longer declared: still reaped when idle, never
#: scaled up. The spaces make it unusable as a label value, so it cannot collide
#: with a real pool name.
UNKNOWN_POOL = "<unknown pool>"


@dataclass
class Pass:
    """Everything one reconcile pass observed, read once at the top.

    The rules below take this instead of a dozen arguments each. It is NOT
    frozen, and the two mutations are the point:

      * rule 0 pops reaped machines out of `machines`/`vnodes`/`ready`/
        `booting`, so no later rule acts on a machine already destroyed;
      * bring_up adds to `peered` as it goes, so the labelling sweeps in the
        same pass can see machines that peered moments earlier.

    Everything else is read-only by convention.
    """
    now: float
    demand: dict
    machines: dict
    credentials: dict
    vnodes: dict
    ready: dict
    booting: dict
    free_on: dict
    kubeconfigs: dict
    pool_name: dict
    pools_by_machine: dict
    flavor_of: dict
    peered: set = field(default_factory=set)

    def in_pool(self, names, pool):
        return [n for n in names
                if self.pool_name.get(n, UNKNOWN_POOL) == pool]


class Controller:
    def __init__(self, hub, liqo, providers, cfg, clock=time.time,
                 namer=lambda: os.urandom(2).hex(), flavor_shapes=None):
        self.hub = hub
        self.liqo = liqo
        self.providers = dict(providers)
        self.cfg = cfg
        self.scale = cfg.scale
        self._clock = clock
        self._namer = namer

        # In-memory, and deliberately so: both are timers, and an operator
        # restart restarting a timer is safe. Persisting them would mean a
        # crash-loop could accumulate a teardown decision across restarts.
        self.idle_since = {}
        self.missing_since = {}
        #: machine -> when it may be peered again, after the scheduler refused
        #: its gateway pod. Without it the abort turns one 10-minute stall into
        #: a ~2.5-minute stall EVERY pass, forever: the machine cannot peer, so
        #: the next pass makes the identical doomed attempt.
        self.blocked_until = {}
        #: Pods already named by the pass-level report. The condition persists
        #: for as long as the machine does, and repeating the same paragraph
        #: every pass is how a reader learns to skip it.
        self.reported_stuck = set()

        #: Providers whose inventory could not be read this pass.
        self._blind = set()

        #: (pool, provider) -> {flavor: Resources}, resolved once at startup.
        #: Empty means nothing is known and packing rents one to measure.
        self._flavor_shapes = dict(flavor_shapes or {})

    # --------------------------------------------------------------- pools --
    def pool_for(self, name):
        """The Pool a machine belongs to, or the unknown-pool stand-in — a
        machine outlives an edit to values, so a missing pool is normal."""
        pool = self.cfg.pool(name)
        if pool is not None:
            return pool
        return Pool(name=UNKNOWN_POOL)

    def _bucket(self, name):
        return name if self.cfg.pool(name) is not None else UNKNOWN_POOL

    def placement(self, pool, provider_name):
        for entry in pool.providers:
            if entry.name == provider_name:
                return entry
        return None

    def shapes_for(self, pool):
        """Every flavor this pool can rent, in priority order.

        Provider order first, then each provider's flavor order, because that is
        the order rent_one and create() try them in. Packing picks the first
        that holds each pod, so a pool listing [d2-4, d2-8] serves a small pod
        with a d2-4 and a 4Gi pod with a d2-8.

        Empty means nothing is known yet, and packing answers that with "rent
        one and measure it".
        """
        out = []
        for entry in pool.providers:
            provider = self.providers.get(entry.name)
            placement = provider.placements.get(pool.name) if provider else None
            if provider is None or placement is None:
                continue
            for flavor, capacity in (
                    self._flavor_shapes.get((pool.name, entry.name)) or {}).items():
                out.append(packing.Shape(
                    entry.name, flavor,
                    pool.schedulable(self._with_pod_capacity(capacity))))
        return out

    def _with_pod_capacity(self, shape):
        """A provider reports cpu, memory and GPUs; how many PODS a machine may
        hold is our policy, not the flavor's.

        Without this the shape has no `pods` dimension, a bin goes to -1 after
        its first pod, and every pod gets a machine of its own.
        """
        if resources.PODS in shape:
            return shape
        return shape + Resources(
            {resources.PODS: self.cfg.liqo.max_pods_per_machine})

    def booting_shape(self, pool, machine_flavor, shapes):
        """What a machine that has not peered yet will arrive with.

        Its recorded flavor when we have one. Falling back to the first flavor
        is what made big pods rent every pass: a d2-8 rented for a 4Gi pod,
        counted as a d2-4, leaves free space the pod does not fit, so the pool
        rents another.
        """
        for shape in shapes:
            if shape.flavor == machine_flavor:
                return shape.vector
        return shapes[0].vector if shapes else Resources()

    def observed_shape(self, pool, ready, pool_name):
        """The largest Ready machine in this pool, which beats any estimate.

        Only used when it is bigger than what the flavor table claims -- a node
        reporting 4 GPUs against a table saying 1 is the table being wrong.
        """
        seen = [v["capacity"] for n, v in ready.items()
                if pool_name.get(n) == pool.name and v["capacity"]]
        if not seen:
            return None
        best = seen[0]
        for c in seen[1:]:
            if best.fits_in(c):
                best = c
        return pool.schedulable(best)

    # ------------------------------------------------------------ machines --
    def machines(self):
        """Every provider's inventory, filtered to what we own.

        Filtered here as well as in each provider: the prefix is the only thing
        standing between this loop and the hand-managed A10 cluster.

        A provider that raises is skipped rather than taking the pass down —
        one cloud's API being unwell must not stop teardown at another.
        """
        out = {}
        for name, provider in self.providers.items():
            try:
                raw = provider.list_machines(self.cfg.name_prefix)
            except Exception as e:                              # noqa: BLE001
                log.error("provider %s: could not list machines (%s); its "
                          "machines are absent from this pass", name, e)
                self._blind.add(name)
                continue
            kept = {n: m for n, m in raw.items()
                    if n.startswith(self.cfg.name_prefix)}
            if raw and not kept:
                # The provider was ASKED for this prefix, so anything discarded
                # here means it ignored the argument -- and discarding ALL of it
                # leaves the rest of the pass indistinguishable from "this cloud
                # has no machines", which is how a whole fleet goes unreapable
                # and keeps billing.
                log.warning("provider %s returned %d machine(s) and NONE match "
                            "the %r it was asked for; ignoring all of them",
                            name, len(raw), self.cfg.name_prefix)
            elif len(kept) != len(raw):
                log.debug("provider %s returned %d machine(s) outside the %r "
                          "prefix; ignoring them",
                          name, len(raw) - len(kept), self.cfg.name_prefix)
            for machine_name, machine in kept.items():
                # Stamped here, not trusted from the provider: reap() routes
                # destroy() on it.
                out[machine_name] = replace(machine, provider=name)
        return out

    def machine_name(self, provider_name):
        """<prefix><provider>-<random>, e.g. external-ovh-1a2b. The pool is
        deliberately not in here — see labels.POOL_LABEL. Nothing matches on the
        provider segment; see Config.name_prefix."""
        return f"{self.cfg.name_prefix}{provider_name}-{self._namer()}"

    def provision(self, pool, entry, flavors=None):
        """Rent one machine for `pool` from `entry`. False when no capacity.

        Credentials are persisted BEFORE the machine is asked for. They exist
        only in memory until that write lands, so a crash in between would leave
        a running, billing box holding CAs whose counterpart no longer exists:
        unreachable, unpeerable, and indistinguishable from one that never
        booted. The reverse ordering only costs a Secret rule 6 already reaps.
        """
        provider = self.providers[entry.name]
        name = self.machine_name(entry.name)
        machine_pki, creds = pki.generate(name)
        self.hub.write_credentials(name, creds, pool=pool.name)
        bootstrap = Bootstrap(
            name=name,
            liqo_version=self.cfg.liqo.version,
            pki=machine_pki,
            hub_egress_ips=self.cfg.hub_egress_ips,
            ssh_public_keys=self.cfg.ssh_public_keys,
            pool=pool.name,
        )
        created = provider.create(bootstrap, provider.placements[pool.name],
                                  flavors=flavors)
        if not created:
            # A definite "nothing was created", so these can go now rather than
            # waiting out rule 6's window. Only on a clean False: an EXCEPTION
            # means we do not know what happened — a timeout after the API
            # accepted the request looks identical — so those are left to rule 6.
            log.info("%s: nothing was created; dropping its credentials", name)
            self.hub.drop_secret(name)
            return False
        # create() returns the flavor it got, which is not necessarily the one
        # we asked for first. Recorded so a machine still booting is sized by
        # what it will actually arrive as.
        if isinstance(created, str):
            self.hub.set_flavor(name, created)
        return True

    def rent_one(self, pool, budget, candidates):
        """One machine for `pool`, trying its providers in priority order.

        `candidates` are the flavors big enough for what this machine is being
        rented for, already in the pool's preference order. "No capacity" moves
        to the next provider -- that is what the fallback list is for. An
        exception ends the pool's turn: we no longer know whether a machine was
        created, and asking a second provider could rent two.
        """
        by_provider = {}
        for shape in candidates:
            names = by_provider.setdefault(shape.provider, [])
            # A None flavor means "whatever the placement lists", which is what
            # create() does when handed nothing.
            if shape.flavor is not None:
                names.append(shape.flavor)
        if not by_provider:
            log.error("pool %s: nothing to rent -- no flavor is big enough for "
                      "what was asked", pool.name)
            return False

        for provider_name, flavors in by_provider.items():
            entry = self.placement(pool, provider_name)
            if entry is None:
                continue
            if budget.get(provider_name, 0) <= 0:
                log.info("pool %s: provider %s has spent its batch budget for "
                         "this pass; the rest follows next pass",
                         pool.name, provider_name)
                continue
            budget[provider_name] -= 1
            try:
                if self.provision(pool, entry, flavors=flavors or None):
                    return True
            except Exception as e:                              # noqa: BLE001
                log.error("pool %s: provisioning from %s failed: %s",
                          pool.name, provider_name, e, exc_info=True)
                return False
            log.info("pool %s: %s has no capacity for %s; trying the next "
                     "provider", pool.name, provider_name,
                     ",".join(flavors) or "any flavor")
        return False

    def peering_pods_stuck_now(self):
        """Every peering pod the hub currently cannot place, read once a pass.

        Returns (pods, readable). `readable` False means the hub could not be
        asked -- NOT that nothing is stuck. Releasing a backoff on that would
        retry a machine on the strength of an API error.

        min_age_s=0: a refusal matters immediately; only the reporting waits.
        """
        try:
            return self.hub.stuck_peering_pods(0), True
        except Exception as e:                                  # noqa: BLE001
            log.warning("could not check for unschedulable peering pods: %s", e)
            return [], False

    def report_stuck_peering_pods(self, stuck, machines):
        """Say so when a peering's pods cannot be scheduled. DIAGNOSTIC ONLY.

        The failure is invisible from where it hurts: liqoctl reports a
        networking error, which reads as a fault on the rented machine, while
        the cause sits in the hub's scheduler written out on a pod nobody looked
        at. So read it out.
        """
        live = {p["name"] for p in stuck}
        # &= runs in-place set intersection, keeps in left only what right has
        self.reported_stuck &= live          # a pod that went away may speak again
        for p in stuck:
            if p["name"] in self.reported_stuck:
                continue
            # A refusal is news at any age; a pod merely Pending is news only
            # once it has waited long enough to mean something.
            if not p["unschedulable"] and p["age_s"] < self.cfg.liqo.stuck_pod_s:
                continue
            if p["machine"] not in machines:
                continue        # residue; force_teardown owns removing it
            log.error("%s: %s Pending %ds -- %s",
                      p["machine"], p["name"], p["age_s"], p["why"])
            self.reported_stuck.add(p["name"])

    def label_virtual_nodes(self, names, pools_by_machine):
        """Stamp each machine's virtual node with its pool's labels and taints.

        Returns the names that are now SETTLED, so a caller sweeping repeatedly
        can stop asking about them. It diffs before writing and tolerates a
        VirtualNode that does not exist yet, so running it early or often is
        cheap and harmless.

        Called from inside the bring-up loop, once per machine brought up, and
        that placement is the whole point: an unlabelled virtual node matches no
        pod's nodeSelector, so until this runs the machine is rented, joined,
        billing -- and useless.
        """
        settled = set()
        for name in sorted(names & set(pools_by_machine)):
            pool = pools_by_machine[name]
            if pool.name == UNKNOWN_POOL:
                # Not settleable and never will be, so retiring it from the
                # sweep is correct rather than merely cheap.
                settled.add(name)
                continue
            try:
                if self.liqo.ensure_node_metadata(name, pool):
                    settled.add(name)
            except Exception as e:                              # noqa: BLE001
                log.warning("%s: could not label the virtual node: %s", name, e)
        return settled

    def reap(self, name, machine, kubeconfigs, peered=(), graceful=True):
        """Full teardown: unpeer (or force) -> release machine -> drop secret.

        Graceful also requires that we actually peered: credentials now exist
        for machines that never came up, and `liqoctl unpeer` against one of
        those burns the whole peer timeout discovering there was nothing to undo.
        """
        kubeconfig = kubeconfigs.get(name)
        torn_down = False
        if graceful and name not in peered:
            log.info("%s: never peered -- skipping graceful unpeer", name)
            graceful = False
        if graceful and kubeconfig:
            try:
                self.liqo.unpeer(name, kubeconfig)
                torn_down = True
            except Exception as e:                              # noqa: BLE001
                log.warning("%s: graceful unpeer failed (%s); forcing", name, e)
        if not torn_down:
            self.liqo.force_teardown(name)
        else:
            # ALWAYS sweep after unpeer, not only when a ForeignCluster is left.
            # unpeer deliberately does not delete the tenant namespace (see
            # Liqo.unpeer), and what it does leave is held by
            # crdreplicator.liqo.io/* finalizers that clear only by talking to
            # the remote. So this has to run NOW, while the machine is still
            # reachable -- the next statement destroys it. force_teardown
            # tolerates 404 on every delete, so a genuinely clean unpeer just
            # makes it a no-op.
            self.liqo.force_teardown(name, expected=True)
        if machine:
            provider = self.providers.get(machine.provider)
            if provider is None:
                log.error("%s: was rented from provider %r, which is no longer "
                          "configured -- the machine CANNOT be released and is "
                          "still billing. Re-add that provider to values, or "
                          "delete the instance by hand.",
                          name, machine.provider)
            else:
                provider.destroy(machine)
        self.hub.drop_secret(name)
        self.idle_since.pop(name, None)
        self.missing_since.pop(name, None)
        self.blocked_until.pop(name, None)
        log.info("%s: reaped", name)

    def reconcile(self):
        """One pass. Level-based: every rule reads live state and converges
        toward it, so a pass that fails part-way is simply redone.
        """
        p = self.observe()
        self.log_pass(p)
        # recycle_condemned mutates the snapshot, popping what it reaps, and
        # must run before the timer rules.
        self.recycle_condemned(p)
        self.bring_up(p)
        self.scale_up(p)
        self.scale_down(p)
        self.reap_dead(p)
        self.reap_boot_timeouts(p)
        self.reap_orphans(p)

    def observe(self):
        """Read the whole world once, so every rule below sees one consistent
        answer rather than re-querying and disagreeing with its neighbour."""
        now = self._clock()
        self._blind = set()

        demand, _unroutable = self.hub.pending_demand()
        machines = self.machines()
        credentials = self.hub.machine_credentials()
        # A mutable COPY, deliberately: the bring-up loop adds to it as it
        # peers, so the labelling sweep after it sees this pass's work.
        peered = set(self.liqo.peerings())

        # The Secret is the only source that exists while a machine is still
        # booting; the virtual node's POOL_LABEL covers a lost Secret.
        # Normalised, or a pool deleted from values matches neither and would be
        # scaled up for by no pool and reaped by no rule.
        pool_name = {n: self._bucket(c.pool)
                     for n, c in credentials.items() if c.pool}
        #: What each machine was rented as, so one still booting is sized by
        #: the flavor we actually got rather than the pool's first.
        flavor_of = {n: c.flavor for n, c in credentials.items() if c.flavor}

        vnodes = self.hub.virtual_nodes()
        for n, v in vnodes.items():
            if n not in pool_name and v.get("pool"):
                pool_name[n] = self._bucket(v["pool"])

        pools_by_machine = {
            n: self.pool_for(pool_name.get(n, UNKNOWN_POOL))
            for n in set(machines) | set(vnodes)}

        # Composed fresh rather than stored: a replaced machine must not be
        # reachable through a URL cached from its predecessor.
        kubeconfigs = {n: c.kubeconfig(machines[n].ip)
                       for n, c in credentials.items()
                       if n in machines and machines[n].ip}

        ready = {n: v for n, v in vnodes.items() if v["ready"]}
        booting = {n: m for n, m in machines.items()
                   if n not in vnodes and now - m.created < self.scale.boot_s}

        # Clamp per node. An oversubscribed machine contributes zero free
        # capacity, never negative.
        free_on = {}
        for n, v in ready.items():
            raw = v["capacity"] - self.hub.used_on(n)
            over = {k: -raw[k] for k in raw.keys() if raw[k] < 0}
            if over:
                log.warning("%s is oversubscribed by %s -- the ResourceSlice is "
                            "not constraining placement", n, over)
            free_on[n] = raw.clamp_zero()

        return Pass(now=now, demand=demand, machines=machines,
                    credentials=credentials, vnodes=vnodes, ready=ready,
                    booting=booting, free_on=free_on, kubeconfigs=kubeconfigs,
                    pool_name=pool_name, pools_by_machine=pools_by_machine,
                    flavor_of=flavor_of, peered=peered)

    def log_flavors(self):
        """Pool shapes, ONCE, at startup.

        They are resolved before the first pass and cannot change while the
        process runs, so repeating them every pass buried the four numbers that
        do change under about eighty characters that never did.
        """
        for pool in self.cfg.pools:
            log.info("pool %s flavors: %s", pool.name,
                     ", ".join(f"{s.flavor}={s.vector}"
                               for s in self.shapes_for(pool))
                     or "unknown -- sized by renting one and measuring it")

    def log_pass(self, p):
        """The one line per pass that says what the fleet looks like, and one
        per pool. Read together they are the whole state of the system."""
        log.info("machines=%d ready=%d booting=%d peered=%d",
                 len(p.machines), len(p.ready), len(p.booting), len(p.peered))
        for pool in self.cfg.pools:
            pending = p.demand.get(pool.name, [])
            # Sizes live in the startup line, not here. The one exception is a
            # pool with none: that state also silences scale_up's "no flavor can
            # hold it" error, so it has to stay visible long after the startup
            # line has scrolled away.
            log.info("  pool %s: pending=%d ready=%d booting=%d machines=%d%s",
                     pool.name, len(pending), len(p.in_pool(p.ready, pool.name)),
                     len(p.in_pool(p.booting, pool.name)),
                     len(p.in_pool(p.machines, pool.name)),
                     "" if self.shapes_for(pool) else " flavors=unknown")
        strays = p.in_pool(p.machines, UNKNOWN_POOL)
        if strays:
            log.info("  %d machine(s) with no declared pool: %s",
                     len(strays), ", ".join(sorted(strays)[:5]))

    # -------------------------------------------------------------- rules --
    def recycle_condemned(self, p):
        """0. Explicit recycle requests, ahead of every timer.

        Pops the machine out of the snapshot as well as reaping it, so no later
        rule in this pass acts on something already destroyed.
        """
        for n in self.hub.condemned():
            if n in p.machines:
                log.info("%s: condemned by operator -- reaping now", n)
                self.reap(n, p.machines[n], p.kubeconfigs, p.peered,
                          graceful=False)
            else:
                log.info("%s: condemned but no such machine -- clearing marker", n)
                self.liqo.force_teardown(n)
                self.hub.drop_secret(n)
            self.hub.unmark_condemned(n)
            p.machines.pop(n, None)
            p.vnodes.pop(n, None)
            p.ready.pop(n, None)
            p.booting.pop(n, None)

    def bring_up(self, p):
        """1. Bring reachable machines all the way to "has capacity".

        Two independent steps -- peer, then advertise a slice -- each checked
        against live state, so a failure in either is retried next pass.
        """
        pending_labels = set(p.peered)
        pending_labels -= self.label_virtual_nodes(pending_labels,
                                                   p.pools_by_machine)

        stuck, hub_readable = self.peering_pods_stuck_now()
        self.report_stuck_peering_pods(stuck, p.machines)
        refused_now = {q["machine"] for q in stuck if q["unschedulable"]}

        sliced = self.liqo.slices()
        for name, kubeconfig in p.kubeconfigs.items():
            if name not in p.machines:
                continue
            if name not in p.peered and not self.liqo.machine_ready(
                    name, kubeconfig):
                continue
            if name not in p.peered and p.now < self.blocked_until.get(name, 0):
                if hub_readable and name not in refused_now:
                    log.info("%s: no longer refused -- retrying", name)
                    self.blocked_until.pop(name, None)
                else:
                    log.info("%s: still refused, %ds left",
                             name, int(self.blocked_until[name] - p.now))
                    continue
            pool = p.pools_by_machine[name]
            try:
                if name not in p.peered:
                    self.liqo.peer(name, kubeconfig)
                    p.peered.add(name)
                if name not in sliced:
                    log.info("%s: peered but no resource slice -- creating", name)
                    self.liqo.ensure_slice(name, kubeconfig, pool)
            except PeeringBlocked:
                self.blocked_until[name] = p.now + self.cfg.liqo.blocked_retry_s
                # Just the decision: report_blocked logs the pod and reason
                # immediately before it raises, and it is a long line.
                log.error("%s: peering refused", name)
            except TenantTerminating as e:
                # A race lost to our own teardown, not a failure -- "bring-up
                # failed" would send the reader after a machine on its way out.
                log.info("%s: %s is terminating -- teardown already under way, "
                         "leaving it alone this pass", name, e)
            except Exception as e:                              # noqa: BLE001
                log.error("%s: bring-up failed: %s", name, e)
            else:
                self.hub.set_endpoint(
                    name, f"https://{p.machines[name].ip}:6443")

            pending_labels |= {name} if name in p.peered else set()
            pending_labels -= self.label_virtual_nodes(pending_labels,
                                                       p.pools_by_machine)

    def scale_up(self, p):
        """2. Rent, per pool. A pod asking for an A10 is not satisfied by free
        capacity on a d2-8, and one number cannot tell them apart."""
        if self.cfg.drain:
            log.info("drain: not renting anything")
            return
        budget = {q.name: max(1, q.batch_size) for q in self.cfg.providers}
        # A machine rented this pass is not yet in `machines`, so each pool
        # would otherwise measure the fleet ceiling against a total that ignores
        # what earlier pools just rented.
        rented = 0
        for pool in self.cfg.pools:
            names = p.in_pool(p.machines, pool.name)
            want = p.demand.get(pool.name, [])
            shapes = self.shapes_for(pool)

            # Per NODE, not summed: a pool can hold machines of different
            # sizes.
            free = [p.free_on[n] for n in p.in_pool(p.ready, pool.name)]
            # A booting machine holds nothing yet, so its whole shape is free.
            free += [self.booting_shape(pool, p.flavor_of.get(n), shapes)
                     for n in p.in_pool(p.booting, pool.name)]

            fit = packing.plan(want, free, shapes)
            for ref, req in fit.never_fits:
                log.error("pool %s: %s requests %s, which no flavor in this "
                          "pool can hold (%s). Not renting for it -- give the "
                          "pool a bigger flavor, or send the pod to one that "
                          "has it.", pool.name, ref, req,
                          ", ".join(f"{s.flavor}={s.vector}" for s in shapes)
                          or "no flavor sizes known")

            # take the pool's first flavor for minMachines.
            need = max(len(fit.bins), pool.min_machines - len(names))
            if need <= 0:
                continue

            # inf when the ceiling is 0 (unlimited), so min() below picks the
            # real constraint without a special case.
            pool_headroom = headroom(pool.max_machines, len(names))
            fleet_headroom = headroom(self.scale.max_machines,
                                      len(p.machines) + rented)
            if pool_headroom <= 0 or fleet_headroom <= 0:
                # "Capped as configured" and "wedged" otherwise look identical.
                # Which cap bound matters: they live in different files.
                limit = (f"pool maxMachines={pool.max_machines}"
                         if pool_headroom <= 0
                         else f"fleet maxMachines={self.scale.max_machines}")
                log.info("pool %s wants %d more machine(s) but %s is reached -- "
                         "not renting. Nothing is stuck.", pool.name, need, limit)
                continue

            wanted = int(min(need, pool_headroom, fleet_headroom))
            if wanted < need:
                log.info("pool %s: %d machine(s) called for, renting %d this "
                         "pass (capped by headroom or batch)",
                         pool.name, need, wanted)
            # minMachines doesn't specify flawor, so keep None
            bins = fit.bins + [None] * max(0, wanted - len(fit.bins))
            for slot in bins[:wanted]:
                if slot is None or slot.shape.flavor is None:
                    # No pod sized this one (a minMachines floor), or nothing is
                    # known about machine sizes yet. Let the provider choose
                    # from its own list.
                    candidates = shapes or [
                        packing.Shape(e.name, None, Resources())
                        for e in pool.providers]
                else:
                    candidates = [s for s in shapes
                                  if packing.fits(slot.used, s.vector,
                                                 set(s.vector.keys()))]
                # rent_one absorbs provider exceptions, so a bad day at one
                # cloud cannot skip the teardown rules below.
                if not self.rent_one(pool, budget, candidates):
                    break
                rented += 1

    def scale_down(self, p):
        """3. Release idle Ready virtual nodes, per pool."""
        for pool_id in [q.name for q in self.cfg.pools] + [UNKNOWN_POOL]:
            pool = self.pool_for(pool_id)
            alive = p.in_pool(p.ready, pool_id) + p.in_pool(p.booting, pool_id)
            floor = pool.min_machines if pool_id != UNKNOWN_POOL else 0
            for n in sorted(p.in_pool(p.ready, pool_id)):
                if len(alive) <= floor:
                    break
                if (self.hub.used_on(n).is_zero()
                        and not p.demand.get(pool_id)):
                    self.idle_since.setdefault(n, p.now)
                    if p.now - self.idle_since[n] > self.scale.idle_s:
                        log.info("reaping idle machine %s (pool %s)", n, pool_id)
                        self.reap(n, p.machines.get(n), p.kubeconfigs, p.peered)
                        alive.remove(n)
                else:
                    self.idle_since.pop(n, None)

    def reap_dead(self, p):
        """4. Corpse cleanup; the replacement is already demand-driven."""
        for n, v in p.vnodes.items():
            if not v["ready"] and p.now - v["since"] > self.scale.dead_s:
                log.warning("reaping dead machine %s", n)
                self.reap(n, p.machines.get(n), p.kubeconfigs, p.peered,
                          graceful=False)

    def reap_boot_timeouts(self, p):
        """5. The machine exists but never became a virtual node."""
        for n, m in p.machines.items():
            if n in p.vnodes or n in p.booting:
                continue
            log.warning("boot timeout for %s -- failed to join within %ds, "
                        "terminating", n, self.scale.boot_s)
            self.reap(n, m, p.kubeconfigs, p.peered, graceful=False)

    def reap_orphans(self, p):
        """6. A peering or Secret whose machine is gone by any path."""
        # A provider we could not reach is NOT a machine that is gone --
        # conflating them tears down a whole cloud's healthy machines over one
        # failed API call.
        if self._blind:
            log.warning("skipping orphan detection: could not list machines "
                        "from %s, so absence proves nothing this pass",
                        ", ".join(sorted(self._blind)))
            return

        for n in list(self.missing_since):
            if n in p.machines:
                log.info("%s: back in the provider inventory -- not an orphan", n)
                self.missing_since.pop(n, None)

        for n in p.peered:
            if n in p.machines:
                continue
            first = self.missing_since.setdefault(n, p.now)
            if p.now - first <= self.scale.missing_confirm_s:
                log.warning("%s: peered but absent from the provider inventory "
                            "-- confirming before teardown", n)
                continue
            log.warning("orphan peering %s -- tearing down", n)
            self.liqo.force_teardown(n)
            self.hub.drop_secret(n)
            self.missing_since.pop(n, None)

        # AGED BY THE SECRET'S OWN creationTimestamp.
        held = []
        for n, c in p.credentials.items():
            if n in p.machines or n in p.peered:
                continue
            # A missing timestamp counts as brand new.
            age = p.now - c.created if c.created else 0
            if age <= self.scale.boot_s:
                held.append(n)
                continue
            log.info("orphan credentials %s (%ds old) -- deleting", n, age)
            self.hub.drop_secret(n)
        if held:
            log.info("%d credential set(s) with no machine yet, holding up to "
                     "%ds each in case a machine is still coming up: %s",
                     len(held), self.scale.boot_s,
                     ", ".join(sorted(held)[:5])
                     + (" ..." if len(held) > 5 else ""))
