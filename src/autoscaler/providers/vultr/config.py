"""Vultr's settings, and the only place their defaults live.

Split the way OVH's is: an ACCOUNT is one API key and its defaults
(VultrConfig); what to rent is per pool (VultrPlacement).

Deliberately not mirrored in the Helm chart: both blocks are opaque passthrough
there, so these defaults exist once. The cost is that Helm can no longer fail on
a missing key — this module does, at startup, naming it.
"""
from dataclasses import dataclass
from pathlib import Path

from .. import cloudinit
from ..base import ProviderConfigError

_CONFIG_KEYS = {"region", "osName", "osId", "provisionScript", "apiUrl"}
_PLACEMENT_KEYS = {"flavors", "gpu", "region"}


def _reject_unknown(cfg, allowed, what):
    unknown = set(cfg) - allowed
    if unknown:
        # A typo'd key would otherwise leave the default silently in force.
        raise ProviderConfigError(
            f"unknown {what} key(s): {sorted(unknown)}; "
            f"expected some of {sorted(allowed)}")


@dataclass(frozen=True)
class VultrConfig:
    """One Vultr account. Shared by every pool renting here."""
    region: str = "ewr"
    #: Resolved to an os_id by substring match, the way OVH resolves an image.
    os_name: str = "Ubuntu 24.04"
    #: Set to skip the lookup entirely — useful when an account sees several
    #: Ubuntu 24.04 variants and the substring match is ambiguous.
    os_id: int = None
    provision_script: str = cloudinit.DEFAULT_SCRIPT
    api_url: str = "https://api.vultr.com/v2"

    @classmethod
    def from_dict(cls, cfg):
        _reject_unknown(cfg, _CONFIG_KEYS, "config")

        os_id = cfg.get("osId")
        if os_id is not None:
            try:
                os_id = int(os_id)
            except (TypeError, ValueError) as e:
                raise ProviderConfigError(
                    f"'osId' must be a number, got {os_id!r}") from e

        script = str(cfg.get("provisionScript", cls.provision_script))
        if not script.startswith("/"):
            script = str(Path(cloudinit.DEFAULT_SCRIPT).parent / script)

        return cls(
            region=str(cfg.get("region", cls.region)),
            os_name=str(cfg.get("osName", cls.os_name)),
            os_id=os_id,
            provision_script=script,
            api_url=str(cfg.get("apiUrl", cls.api_url)).rstrip("/"),
        )


@dataclass(frozen=True)
class VultrPlacement:
    """What one pool rents from this account.

    `flavors`, not Vultr's own word "plan": the key an operator fills in means
    the same thing in every provider's block, and the translation to provider
    vocabulary happens in provider.py where the API call is made.
    """
    # Priority order; the first flavor with stock wins. Not validated against
    # the pool's name — a pool called gpu-l40s listing a CPU flavor as a
    # fallback is the operator's call.
    flavors: tuple = ()
    # False skips the whole NVIDIA stack.
    gpu: bool = True
    #: Vultr stocks GPU plans in a handful of regions only, so a GPU pool is
    #: usually a region override.
    region: str = None

    @classmethod
    def from_dict(cls, cfg):
        _reject_unknown(cfg, _PLACEMENT_KEYS, "pool provider")

        flavors = cfg.get("flavors", cls.flavors)
        if isinstance(flavors, str):
            # A bare string would iterate character by character and try to rent
            # a flavor called "v".
            flavors = [f.strip() for f in flavors.split(",") if f.strip()]
        if not flavors:
            raise ProviderConfigError("'flavors' is empty — nothing to rent")

        region = cfg.get("region")
        return cls(
            flavors=tuple(str(f) for f in flavors),
            gpu=bool(cfg.get("gpu", cls.gpu)),
            region=str(region) if region else None,
        )
