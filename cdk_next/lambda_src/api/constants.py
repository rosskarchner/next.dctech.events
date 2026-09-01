"""Shared env-var-with-default literals used across multiple Lambda
deployment units (api, newsletter).

Copied into each unit's build directory by build_lambdas.sh, the same way
db.py already is — these are source files shared across separately-packaged
Lambdas, not a single importable dependency, since api/ and newsletter/ each
build their own independent deployment zip.
"""
import os

CONTACT_LIST_NAME = os.environ.get('CONTACT_LIST_NAME', 'newsletters')
NEWSLETTER_TOPIC = os.environ.get('NEWSLETTER_TOPIC', 'dctech')
REPLY_TO_EMAIL = os.environ.get('REPLY_TO_EMAIL', 'ross@karchner.com')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'outbound@dctech.events')
