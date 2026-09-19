from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Stands in for runserver and the worker commands: it would never end in production."""

    def handle(self, *args, **options):
        return None
