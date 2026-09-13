"""Connector registry.

Adding a source is: write the module, add one line here.
"""

from __future__ import annotations

from pathlib import Path

from jobscout.connectors.base import DumpStore, Secrets, SourceConnector
from jobscout.connectors.infojobs_apify import InfoJobsApifyConnector
from jobscout.connectors.linkedin_apify import LinkedInApifyConnector
from jobscout.connectors.rss import RssConnector
from jobscout.connectors.xarxanet import XarxanetConnector
from jobscout.profile import UserProfile

__all__ = [
    "DumpStore",
    "Secrets",
    "SourceConnector",
    "build_connectors",
    "InfoJobsApifyConnector",
    "LinkedInApifyConnector",
    "RssConnector",
    "XarxanetConnector",
]


def build_connectors(
    profile: UserProfile, dumps_dir: Path | str = "dumps"
) -> dict[str, SourceConnector]:
    """One connector instance per source type, for this profile.

    Apify connectors need the profile id and the dump directory so that two
    users replaying dumps cannot read each other's data.
    """
    dumps = DumpStore(dumps_dir)
    return {
        "rss": RssConnector(),
        "xarxanet": XarxanetConnector(),
        "infojobs_apify": InfoJobsApifyConnector(profile.profile_id, dumps),
        "linkedin_apify": LinkedInApifyConnector(profile.profile_id, dumps),
    }
