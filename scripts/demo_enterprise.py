"""Explicitly add enterprise demo roles to the project's known synthetic accounts."""
from sqlalchemy import select
from packages.domain.db import transaction
from packages.domain.models import User,Membership,Audit
from packages.domain.auth import verify_password


def main():
    with transaction(write=True) as s:
        for domain in ('demo','other'):
            for role,extra in [('admin',['publisher','supervisor','integration_manager','adjudicator']),('operator',['annotator']),('reviewer',['annotator'])]:
                user=s.scalar(select(User).where(User.email==f'{role}@{domain}.local'))
                if not user or not verify_password('Demo2026!match',user.password_hash):continue
                for member in s.scalars(select(Membership).where(Membership.user_id==user.id)):
                    member.roles=sorted(set(member.roles+extra));s.add(Audit(org_id=member.org_id,actor_id=user.id,action='demo.enterprise_roles',resource_id=member.id,detail={'added':extra,'scope':'synthetic demo accounts only'},request_id='demo-enterprise'))
    print('Updated only known synthetic demo accounts; administrator remains separate from reviewer.')


if __name__=='__main__':main()
