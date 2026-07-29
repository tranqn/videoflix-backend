"""Settings module for pytest-django (DJANGO_SETTINGS_MODULE=core.settings_test).

pytest never puts ``'test'`` into ``sys.argv``, so ``core.settings`` alone
would try to talk to real Postgres/Redis/SMTP. Setting DJANGO_TESTING=1
*before* importing ``core.settings`` makes its ``TESTING`` flag true, which
triggers the same sqlite/locmem/sync-RQ overrides ``manage.py test`` gets.
"""

import os

os.environ['DJANGO_TESTING'] = '1'

from .settings import *  # noqa: E402, F403
