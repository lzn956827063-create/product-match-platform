"""Create a fresh organization's initial admin; password is read without echo."""
import argparse
import getpass
from sqlalchemy import select
from packages.domain.auth import password_hash
from packages.domain.db import initialize,transaction,uid
from packages.domain.models import User,Organization,Membership,Policy
from packages.matching.engine import DEFAULT_POLICY


def main():
    p=argparse.ArgumentParser();p.add_argument('--org',required=True);p.add_argument('--email',required=True);p.add_argument('--name',required=True);a=p.parse_args()
    password=getpass.getpass('Initial administrator password (at least 10 characters): ')
    if len(password)<10:raise SystemExit('Password too short')
    if password!=getpass.getpass('Repeat password: '):raise SystemExit('Passwords do not match')
    initialize()
    with transaction(write=True) as s:
        if s.scalar(select(User).where(User.email==a.email.strip().lower())):raise SystemExit('Account unavailable')
        org=Organization(id=uid(),name=a.org);u=User(id=uid(),email=a.email.strip().lower(),name=a.name,password_hash=password_hash(password))
        s.add_all([org,u]);s.flush();policy=Policy(id=uid(),org_id=org.id,name='手机 SKU 规则基线 v1',config=DEFAULT_POLICY);s.add(policy);org.default_policy_id=policy.id;s.add(Membership(org_id=org.id,user_id=u.id,roles=['admin']));s.flush()
        print('Created organization and administrator:',org.id)


if __name__=='__main__':main()
