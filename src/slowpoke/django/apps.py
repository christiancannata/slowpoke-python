from django.apps import AppConfig


class SlowpokeConfig(AppConfig):
    name = "slowpoke.django"
    label = "slowpoke"
    verbose_name = "Slowpoke"

    def ready(self):
        from . import install

        install()
