"""Idempotently materialize historical deltas and quality without changing old artifact bytes."""
from sqlalchemy import select
from packages.domain.db import transaction
from packages.domain.models import BatchRelease,Revision
from workers.releases import process_files
from workers.quality import process_quality


def main():
    with transaction() as s:
        releases=list(s.scalars(select(BatchRelease.id).where(BatchRelease.published_at.is_not(None))));revisions=list(s.scalars(select(Revision.id)))
    for ident in releases:process_files(ident)
    for ident in revisions:process_quality(ident)
    print({'historical_releases':len(releases),'quality_revisions':len(revisions)})

if __name__=='__main__':main()
