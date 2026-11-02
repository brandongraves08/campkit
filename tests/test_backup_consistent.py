import os
import subprocess
from pathlib import Path


def test_consistent_backup_quiesces_running_web_and_restarts_it(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    event_log = tmp_path / "events.log"
    state_file = tmp_path / "web-stopped"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "STATE=$CAMPKIT_BACKUP_TEST_STATE\n"
        "printf '%s\\n' \"$*\" >> \"$CAMPKIT_BACKUP_TEST_LOG\"\n"
        "case \"$*\" in\n"
        "  *'ps --status running --services'*) if [ -f \"$STATE\" ]; then printf 'db\\n'; else printf 'db\\nweb\\n'; fi ;;\n"
        "  *'stop web'*) : > \"$STATE\"; printf 'web-stop\\n' >> \"$CAMPKIT_BACKUP_TEST_LOG\" ;;\n"
        "  *'start web'*) rm -f \"$STATE\"; printf 'web-start\\n' >> \"$CAMPKIT_BACKUP_TEST_LOG\" ;;\n"
        "  *'exec -T db'*) printf 'PGDMPfake-archive' ;;\n"
        "  *'exec -T web'*) printf 'web must remain stopped while backing up media\\n' >&2; exit 64 ;;\n"
        "  *'run --rm --no-deps -T --entrypoint /bin/sh web -c'*) tar --create --gzip --file - --files-from /dev/null ;;\n"
        "  *) printf 'unexpected docker invocation: %s\\n' \"$*\" >&2; exit 64 ;;\n"
        "esac\n"
    )
    fake_docker.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "CAMPKIT_BACKUP_TEST_LOG": str(event_log),
        "CAMPKIT_BACKUP_TEST_STATE": str(state_file),
        "CAMPKIT_BACKUP_DIR": str(tmp_path / "backups"),
        "CAMPKIT_BACKUP_RETENTION_DAYS": "14",
    }
    result = subprocess.run(
        ["bash", str(repo / "deploy/backup-consistent.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    events = event_log.read_text().splitlines()
    stop_index = events.index("web-stop")
    start_index = events.index("web-start")
    dump_index = next(i for i, event in enumerate(events) if "exec -T db" in event)
    media_index = next(i for i, event in enumerate(events) if "run --rm --no-deps -T --entrypoint /bin/sh web -c" in event)
    assert stop_index < dump_index < media_index < start_index
    assert list((tmp_path / "backups").glob("campkit-*.sha256"))
