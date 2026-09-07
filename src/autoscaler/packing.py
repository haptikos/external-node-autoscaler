"""How many machines a set of pending pods needs, and of which flavor.

First-fit-decreasing over resource vectors, across every flavor a pool can rent.

Flavors are tried in the pool's declared order and the first that holds the pod
wins, so a pool listing [d2-4, d2-8] serves a small pod with a d2-4 and a 4Gi
pod with a d2-8. A pod that fits NO flavor is impossible, not merely awkward,
and is reported rather than rented for.

FFD is an approximation, deliberately. It is within a small constant of optimal
for this shape of problem and short enough to read.
"""
from collections import namedtuple

from . import logging_setup
from .resources import Resources

log = logging_setup.get("packing")

#: One kind of machine a pool can rent. `vector` is what the scheduler may use
#: after reserves, not the raw hardware.
Shape = namedtuple("Shape", "provider flavor vector")


def fits(want, capacity, measured):
    """Does `want` fit `capacity`, enforcing only `measured` dimensions?

    `measured` is the set of dimensions the SHAPE reports, captured before
    anything is subtracted. Resources drops zeroes, so a dimension a bin has
    exhausted vanishes from the vector and would otherwise be indistinguishable
    from one the provider never reported. Enforcing an unreported dimension
    means no pod fits any bin and the pool rents a machine per pod; ignoring an
    exhausted one means a bin accepts pods forever.
    """
    for name in want.keys():
        if name not in measured:
            continue
        if want[name] > capacity.get(name, 0):
            return False
    return True


class Bin:
    """One machine we intend to rent, and what it is expected to hold."""

    def __init__(self, shape):
        self.shape = shape
        self.used = Resources()

    @property
    def measured(self):
        return set(self.shape.vector.keys())

    def remaining(self):
        return self.shape.vector - self.used

    def take(self, want):
        self.used = self.used + want


class Plan:
    def __init__(self, bins=(), placed_existing=0, never_fits=()):
        #: Bins to rent, in the order they were opened.
        self.bins = list(bins)
        #: Pods that fit into headroom on machines already running.
        self.placed_existing = placed_existing
        #: (pod ref, requests) that no flavor in the pool can hold.
        self.never_fits = list(never_fits)


def plan(pods, free_on_existing, shapes):
    """`pods` is (ref, Resources). `free_on_existing` is per-node free vectors.
    `shapes` is every flavor the pool can rent, in priority order.

    An empty `shapes` means nothing is known about machine size yet: one bin,
    and the caller learns the real shape once it peers.
    """
    if not pods:
        return Plan()

    # Awkward pods first: placing the big ones while bins are empty is what
    # makes first-fit behave. Ranked against the first flavor, so a pod that
    # exceeds it sorts to the front and picks a bigger flavor before the small
    # ones have scattered across bins.
    reference = shapes[0].vector if shapes else Resources()
    ordered = sorted(pods, key=lambda kv: kv[1].dominant_ratio(reference),
                     reverse=True)

    existing = list(free_on_existing)
    bins = []
    placed_existing = 0
    never_fits = []

    for ref, want in ordered:
        if shapes and not any(
                fits(want, s.vector, set(s.vector.keys())) for s in shapes):
            # No flavor in this pool can hold it, so no number of machines will.
            never_fits.append((ref, want))
            continue

        # Machines already running report complete vectors, so an absent
        # dimension there really is zero.
        for i, free in enumerate(existing):
            if want.fits_in(free, known_only=False):
                existing[i] = free - want
                placed_existing += 1
                break
        else:
            for b in bins:
                if fits(want, b.remaining(), b.measured):
                    b.take(want)
                    break
            else:
                if not shapes:
                    return Plan(bins=[Bin(Shape(None, None, Resources()))],
                                placed_existing=placed_existing)
                # The first declared flavor that holds it: the pool's order is
                # its preference, usually cheapest first.
                shape = next(s for s in shapes
                             if fits(want, s.vector, set(s.vector.keys())))
                b = Bin(shape)
                b.take(want)
                bins.append(b)

    return Plan(bins=bins, placed_existing=placed_existing,
                never_fits=never_fits)
