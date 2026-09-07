"""The contract a node source has to satisfy.

A provider's entire job is: **make a machine exist that runs the join
bootstrap.** From the moment that machine marks itself ready, everything else —
peering, capacity advertisement, idle detection, teardown — is hub-side and
knows nothing about where the machine came from. That is why this interface is
four methods and not forty.

An ABC rather than a Protocol on purpose: a provider missing a method should
fail when it is imported, not on the first scale-up at 3am.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

DEFAULT_BATCH_SIZE = 50


@dataclass(frozen=True)
class Machine:
    """One rented machine, in terms the core understands.

    `created` is epoch seconds because every provider spells timestamps
    differently and the boot-timeout arithmetic should not have to care. The
    provider normalises it; nothing downstream parses a date.
    """
    name: str        # authoritative identity — see Provider.create
    id: str          # provider-native handle, opaque to the core
    status: str      # provider-native string, for logs only, never branched on
    created: float   # epoch seconds
    #: Routable address the hub reaches this machine's API on. None is normal
    #: for a freshly created instance, not an error: it is simply not peered
    #: this pass. The provider picks it — public, IPv4, not RFC1918.
    ip: str = None
    #: Stamped by the core in Controller.machines, not by the provider, because
    #: destroy() is routed on it: a wrong value sends the delete to a cloud that
    #: never heard of the machine while the real box keeps billing.
    provider: str = None


@dataclass(frozen=True)
class Bootstrap:
    """Everything a new machine needs to stand itself up, provider-neutral.

    Deliberately fields rather than a pre-rendered blob: a raw-VM provider turns
    this into cloud-init user data (see cloudinit.py), while one that boots a
    machine over SSH turns it into a setup script. Rendering it here would force
    every provider into the first shape.

    The machine never calls the hub: the operator pre-generates its certificate
    authorities and assembles the kubeconfig itself. See ../pki.py.

    `pki` maps absolute on-machine path -> single-line base64, keyed by
    cloudinit.K3S_CA_FILES. `repr=False` is not cosmetic — it holds five CA
    private keys, which a bare repr() in a traceback would print.
    """
    name: str
    liqo_version: str
    pki: dict = field(repr=False, default_factory=dict)
    hub_egress_ips: tuple = ()
    ssh_public_keys: tuple = ()
    #: Cosmetic on the machine itself — it becomes a node label so `kubectl get
    #: nodes -L` on the box says what it was for. The pool that COUNTS is the one
    #: on the operator's Secret; see labels.POOL_LABEL.
    pool: str = ""


class ProviderConfigError(Exception):
    """Raised by from_config when the mounted config cannot be used.

    Separate from a generic exception so __main__ can report it as a
    configuration problem — the operator will crash-loop, and the log line is
    the only thing telling anyone which key is wrong.
    """


class Provider(ABC):
    #: Set by the loader from the directory name. Used in log lines, and as a
    #: segment of every machine name.
    name = "unnamed"

    #: pool name -> whatever parse_placement returned, filled by the loader at
    #: startup. create() is handed the right one.
    placements = {}

    @classmethod
    @abstractmethod
    def from_config(cls, cfg):
        """Build from the `config:` subtree of a `providers:` entry.

        Account-level settings only — credentials, region, image. What to rent
        is per pool; see parse_placement.

        Raise ProviderConfigError with the offending key named. This runs at
        startup, so a bad value costs a crash-loop and not a rented machine.
        """

    @classmethod
    def parse_placement(cls, cfg):
        """Validate one pool's entry for this provider, at STARTUP — so a typo
        is a crash-loop naming the key, not a scale-up that quietly does nothing
        on the pass capacity was needed.

        `cfg` is that entry minus `name` and `priority`. The returned object is
        opaque to the core; what the loop needs about machine size comes from
        flavor_capacity, not from this.
        """
        raise ProviderConfigError(
            f"provider {cls.__name__} cannot be used in a pool: it does not "
            f"implement parse_placement")

    def flavor_capacity(self, placement):
        """{flavor name -> Resources} for what this placement can rent.

        Resolved once at startup, in the flavor list's priority order — the
        first key is what create() will try first and therefore what new
        machines are sized on.

        A dimension this provider cannot report must be OMITTED, never set to
        zero. Absent means unmeasured and the loop rents one to find out; zero
        means the machine genuinely has none, which would make every pod asking
        for it permanently unschedulable.
        """
        raise ProviderConfigError(
            f"provider {type(self).__name__} cannot size a pool: it does not "
            f"implement flavor_capacity")

    @abstractmethod
    def list_machines(self, name_prefix):
        """name -> Machine, for machines this operator created.

        The prefix is passed in so a provider can filter server-side where its
        API allows it. The core filters the result again regardless: a provider
        that forgets would otherwise hand back somebody else's instances from a
        shared account, and every reap path acts on what this returns.
        """

    @abstractmethod
    def create(self, bootstrap, placement, flavors=None):
        """Bring up one machine that runs the bootstrap.

        Returns the FLAVOR NAME it rented (truthy) so the core can record what
        it actually got; a machine still booting has no node to measure, and
        assuming the wrong size makes big pods rent a machine every pass.

        `placement` is what parse_placement returned for the pool being scaled.
        `flavors`, when given, restricts and orders the candidates: the core
        passes only those big enough for the pods this machine is for, so stock
        fallback cannot hand back one too small to run them.

        Return **False** for "no capacity available" — out of stock, quota
        exhausted — and the loop moves to this pool's next provider, then
        retries next pass. A stock shortage must not read like an outage: that
        is the one distinction this contract insists on.

        For anything else, **raise** or log-and-return-False, whichever suits
        the API. Raising gives a traceback the reconcile loop catches per pool;
        logging keeps the pool trying its remaining flavors. Providers here do
        both — ovh raises, vultr logs — because one has few failure modes worth
        naming and the other has many not worth a lookup table.

        The name in `bootstrap` is the machine's identity everywhere: k3s node
        name, Liqo cluster id, tenant namespace suffix, virtual node name. Apply
        it verbatim. A provider that renames, truncates or lowercases it breaks
        every teardown path, and does so silently until a machine dies.
        """

    @abstractmethod
    def destroy(self, machine):
        """Release the machine. Must tolerate one that is already gone."""

    def validate(self):
        """Optional startup check: credentials work, image exists, quota is sane.

        Runs once before the first reconcile so a misconfiguration is a startup
        failure rather than a scale-up failure.
        """
