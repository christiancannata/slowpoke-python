from django.core.management.base import BaseCommand

from apps.django_shop.models import Order


class Command(BaseCommand):
    """The kind of command a server runs from cron every night."""

    help = "Closes the orders of the day"

    def add_arguments(self, parser):
        parser.add_argument("--fail", action="store_true")

    def handle(self, *args, **options):
        list(Order.objects.all()[:5])  # origin: command
        if options.get("fail"):
            raise ValueError("the nightly job broke")
