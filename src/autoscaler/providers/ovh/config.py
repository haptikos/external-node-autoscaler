"""OVH's settings, and the only place their defaults live.

Split the way the fleet file is: an ACCOUNT is one project and one set of
credentials (OvhConfig); what to rent is per pool (OvhPlacement).

Deliberately not mirrored in the Helm chart: both blocks are opaque passthrough
there, so these defaults exist once. The cost is that Helm can no longer fail on
a missing `project` — this module does, at startup, naming the key.
"""
from dataclasses import dataclass
from pathlib import Path

from .. import cloudinit
from ..base import ProviderConfigError

_CONFIG_KEYS = {"project", "region", "imageName", "provisionScript"}
_PLACEMENT_KEYS = {"flavors", "gpu", "region"}


def _reject_unknown(cfg, allowed, what):
    unknown = set(cfg) - allowed
    if unknown:
        # A typo'd key would otherwise leave the default silently in force.
        raise ProviderConfigError(
            f"unknown {what} key(s): {sorted(unknown)}; "
            f"expected some of {sorted(allowed)}")


@dataclass(frozen=True)
class OvhConfig:
    """One OVH project. Account-level, shared by every pool renting here."""
    project: str
    region: str = "GRA11"
    image_name: str = "Ubuntu 24.04"
    # The shared provisioning script (providers/provision.sh). An absolute path
    # is honoured as given, which is the hot-patch escape hatch (mount a
    # ConfigMap over it) and what lets the tests point at /dev/null; a relative
    # one resolves against providers/, so a provider can ship its own.
    provision_script: str = cloudinit.DEFAULT_SCRIPT

    @classmethod
    def from_dict(cls, cfg):
        _reject_unknown(cfg, _CONFIG_KEYS, "config")
        if not cfg.get("project"):
            raise ProviderConfigError(
                "'project' is required (the 32-char hex OVH public cloud "
                "project id); set it under this provider's config in values")

        script = str(cfg.get("provisionScript", cls.provision_script))
        if not script.startswith("/"):
            script = str(Path(cloudinit.DEFAULT_SCRIPT).parent / script)

        return cls(
            project=str(cfg["project"]),
            region=str(cfg.get("region", cls.region)),
            image_name=str(cfg.get("imageName", cls.image_name)),
            provision_script=script,
        )


@dataclass(frozen=True)
class OvhPlacement:
    """What one pool rents from this project."""
    # Priority order; the first flavor with stock wins.
    flavors: tuple = ()
    # False skips the whole NVIDIA stack.
    gpu: bool = True
    #: OVH stocks flavors unevenly across regions, so a second pool is often a
    #: second region.
    region: str = None

    @classmethod
    def from_dict(cls, cfg):
        _reject_unknown(cfg, _PLACEMENT_KEYS, "pool provider")

        flavors = cfg.get("flavors", cls.flavors)
        if isinstance(flavors, str):
            # A bare string would iterate character by character and try to rent
            # a flavor called "a".
            flavors = [f.strip() for f in flavors.split(",") if f.strip()]
        if not flavors:
            raise ProviderConfigError("'flavors' is empty — nothing to rent")

        region = cfg.get("region")
        return cls(
            flavors=tuple(str(f) for f in flavors),
            gpu=bool(cfg.get("gpu", cls.gpu)),
            region=str(region) if region else None,
        )
