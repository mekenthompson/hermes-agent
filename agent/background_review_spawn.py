"""Start the existing review thread with its native producer reservation."""

import threading


def spawn_background_review_now(agent, messages_snapshot, review_memory=False, review_skills=False,
                                focus=None, task_cfg=None, _requeue_attempts=0, explicit=False):
    from agent.background_review import (
        finish_background_review_run, prepare_background_review_run, spawn_background_review_thread,
    )
    from agent.memory_task_authority import bind_memory_task
    from tools.approval_context import approval_task_revoked
    from tools.thread_context import propagate_context_to_thread

    if approval_task_revoked():
        return
    review_run = prepare_background_review_run(agent)
    if review_run is None:
        return
    release = lambda *_: None
    try:
        target, _prompt = spawn_background_review_thread(
            agent, messages_snapshot, review_memory=review_memory, review_skills=review_skills,
            focus=focus, task_cfg=task_cfg, review_run=review_run, explicit=explicit)

        def target_with_requeue():
            target()
            agent._maybe_requeue_preempted_review(review_run, dict(
                messages_snapshot=messages_snapshot, review_memory=review_memory, review_skills=review_skills,
                focus=focus, task_cfg=task_cfg, _requeue_attempts=_requeue_attempts + 1, explicit=explicit))

        guarded, release = bind_memory_task(target_with_requeue)
        if guarded is None:
            finish_background_review_run(agent, review_run)
            return

        def owned_target():
            try:
                guarded()
            finally:
                # A Stop before the worker starts skips the body, including its cleanup.
                finish_background_review_run(agent, review_run)

        # health: allow HX012 -- the target already propagates context; preserve the existing Thread constructor test seam
        threading.Thread(target=propagate_context_to_thread(owned_target), daemon=True, name="bg-review").start()
    except BaseException:
        release()
        finish_background_review_run(agent, review_run)
        raise
