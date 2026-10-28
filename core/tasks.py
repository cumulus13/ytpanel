from celery import shared_task

from .services import purge as purge_service
from .services import scheduler, worker


@shared_task(name="core.tasks.run_download_task")
def run_download_task(dl_id, token):
    return worker.run_download(dl_id, token)


@shared_task(name="core.tasks.scheduler_tick")
def scheduler_tick():
    started = scheduler.tick()
    scheduler.record_speed()
    return started


@shared_task(name="core.tasks.reaper")
def reaper():
    return scheduler.reap()


@shared_task(name="core.tasks.auto_purge")
def auto_purge():
    return purge_service.auto_purge()
