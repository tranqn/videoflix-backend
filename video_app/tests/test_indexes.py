"""Tests for the composite index backing the video list query."""

from django.db import connection
from django.test import TestCase

from video_app.models import Video

INDEX_NAME = 'video_status_created_idx'


class VideoIndexTests(TestCase):
    """The (status, -created_at) index must be declared and actually created."""

    def test_index_is_declared_on_the_model(self):
        declared = {index.name: index.fields for index in Video._meta.indexes}
        self.assertEqual(declared.get(INDEX_NAME), ['status', '-created_at'])

    def test_index_exists_in_the_database(self):
        """Proves the migration ran, not just that the model declares it."""
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, Video._meta.db_table)
        self.assertIn(INDEX_NAME, constraints)
        constraint = constraints[INDEX_NAME]
        self.assertTrue(constraint['index'])
        self.assertEqual(constraint['columns'], ['status', 'created_at'])
        self.assertFalse(constraint['unique'])
