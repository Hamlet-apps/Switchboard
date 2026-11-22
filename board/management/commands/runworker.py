import time

from django.core.management.base import BaseCommand

from board import worker


class Command(BaseCommand):
    help = 'Run queued workflow runs and due schedules. Use with SWITCHBOARD_WORKER=off on the web processes.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Do the waiting work and exit.')

    def handle(self, *args, **options):
        if options['once']:
            worker.loop(forever=False)
            return
        self.stdout.write('Switchboard worker running. Ctrl+C stops it.')
        while True:
            worker.loop(forever=False)
            time.sleep(5)
