"""Shared base class for the auth tests."""

from django.core.cache import cache
from rest_framework.test import APITestCase


class ThrottledAPITestCase(APITestCase):
    """APITestCase that starts and ends each test with empty throttle buckets.

    DRF stores throttle history in the cache, and the test cache is LocMem,
    which lives as long as the process rather than as long as one test. Without
    clearing it, requests made by one test count against the rate limit of the
    next one and the suite starts failing depending on execution order.

    Subclasses that override ``setUp``/``tearDown`` must call ``super()``.
    """

    def setUp(self):
        super().setUp()
        cache.clear()

    def tearDown(self):
        cache.clear()
        super().tearDown()
