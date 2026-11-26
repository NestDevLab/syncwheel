"""Automatic startup update decisions, separate from explicit self commands."""
from syncwheel_errors import SyncwheelError


def handle_startup_update_policy(args, *, load_settings, refresh_status, perform_update,
                                 emit, current_version, recommended_command):
    if getattr(args, 'command', None) == 'self':
        return
    try:
        settings = load_settings()
        if settings['mode'] == 'off':
            return
        status, settings, _, _ = refresh_status(force=False, settings=settings)
    except SyncwheelError:
        return
    if not status.get('update_available'):
        return
    current_version = status.get('current_version') or current_version
    latest_version = status.get('latest_version') or current_version
    if settings['mode'] == 'auto':
        try:
            before, after, _ = perform_update(fetch=True)
            emit(
                f'syncwheel auto-updated {before["current_version"]} -> {after["current_version"]}',
            )
            return
        except SyncwheelError as exc:
            emit(
                'NOTICE: syncwheel update available '
                f'({current_version} -> {latest_version}) but auto-update was blocked: {exc}. '
                f'Run: {status.get("recommended_command") or recommended_command()}',
            )
            return
    emit(
        f'NOTICE: syncwheel update available ({current_version} -> {latest_version}). '
        f'Run: {status.get("recommended_command") or recommended_command()}',
    )
