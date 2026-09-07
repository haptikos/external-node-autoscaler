"""Provider discovery.

Providers are found by **listing directories**, not by a registry anyone has to
remember to update: a directory containing `provider.py` is a provider, and the
directory name is the provider name. Adding a node source is creating a
directory and naming it in values — no edit to this file, the core, or the chart.

Discovery does not import. Only the selected provider is imported, so an
unselected one's dependencies can be absent from the image without stopping
the operator from starting. That is also why a directory with no provider.py
is reported as unavailable rather than blowing up on import.
"""
import importlib
import pathlib
import re

from .. import logging_setup

from .base import Bootstrap, Machine, Provider, ProviderConfigError

# A provider's directory name becomes a segment of every machine name, and a
# machine name becomes a Kubernetes object name and a Liqo cluster id. So the
# directory name has to survive that: lowercase alphanumerics and hyphens.
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

log = logging_setup.get("providers")

__all__ = ["Bootstrap", "Machine", "Provider", "ProviderConfigError",
           "available", "flavor_shapes", "load", "load_all"]


def available():
    here = pathlib.Path(__file__).parent
    return sorted(p.name for p in here.iterdir()
                  if p.is_dir() and (p / "provider.py").exists())


def load(name, cfg):
    """Import the named provider and build it from its config subtree."""
    known = available()
    if name not in known:
        raise SystemExit(f"unknown provider {name!r}; available: {known}")
    if not _DNS_LABEL.match(name):
        raise SystemExit(
            f"provider directory {name!r} is not usable in a machine name: "
            f"lowercase letters, digits and hyphens only")
    module = importlib.import_module(f"{__package__}.{name}")
    try:
        provider = module.PROVIDER.from_config(cfg)
    except ProviderConfigError as e:
        raise SystemExit(f"provider {name}: {e}") from e
    provider.name = name
    return provider


def load_all(specs, pools):
    """Every configured provider, with each pool's placement parsed at startup
    — a bad flavor list should be a crash-loop naming the pool, not a scale-up
    that quietly fails when capacity was wanted."""
    providers = {s.name: load(s.name, s.config) for s in specs}
    for spec in specs:
        providers[spec.name].batch_size = spec.batch_size

    for pool in pools:
        for entry in pool.providers:
            provider = providers[entry.name]
            try:
                parsed = type(provider).parse_placement(entry.config)
            except ProviderConfigError as e:
                raise SystemExit(
                    f"pool {pool.name!r}, provider {entry.name!r}: {e}") from e
            # Per instance, not per class, or two pools renting different
            # flavors would share the last one parsed.
            if type(provider).placements is provider.placements:
                provider.placements = {}
            provider.placements[pool.name] = parsed
    return providers


def _sanity_check_shapes(pool, provider_name, shapes):
    """A shape missing cpu or memory is almost always a unit mistake.

    It is not fatal -- an unmeasured dimension is a legitimate state, and
    packing handles it by renting one machine and measuring it. But cpu and
    memory come from every provider's API, so their absence means the numbers
    were parsed wrong, and the symptom is a pool that rents far more than it
    needs before anyone looks.
    """
    from ..resources import CPU, MEMORY
    for flavor, shape in shapes.items():
        missing = [d for d in (CPU, MEMORY) if d not in shape]
        if missing:
            log.warning(
                "pool %s: %s reports flavor %s with no %s (%s). Check the "
                "provider's units -- a shape smaller than the pool's reserve "
                "clamps away entirely.",
                pool.name, provider_name, flavor, " or ".join(missing), shape)


def flavor_shapes(providers, pools):
    """(pool, provider) -> {flavor: Resources}, resolved once at startup.

    Here rather than in the loop because it costs a provider API call and the
    answer does not change between passes. A provider that cannot report sizes
    contributes nothing and its pools fall back to renting one and measuring it.
    """
    out = {}
    for pool in pools:
        for entry in pool.providers:
            provider = providers.get(entry.name)
            placement = provider.placements.get(pool.name) if provider else None
            if placement is None:
                continue
            try:
                shapes = provider.flavor_capacity(placement)
            except Exception as e:                              # noqa: BLE001
                log.warning("pool %s: %s cannot report flavor sizes (%s); its "
                            "machines will be sized by renting one",
                            pool.name, entry.name, e)
                continue
            if shapes:
                _sanity_check_shapes(pool, entry.name, shapes)
                out[(pool.name, entry.name)] = shapes
    return out
