from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def mock_config() -> str:
    return r'''#!/usr/bin/env bash
AWS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AWS_DIR/../.." && pwd)"
: "${STATE_FILE:?STATE_FILE is required by the fixture}"
LOCAL_KEY="$AWS_DIR/mock-key"
KNOWN_HOSTS_FILE="$AWS_DIR/mock-known-hosts"
REGION=test-region
INSTANCE_TYPE=test-server
LOADGEN_INSTANCE_TYPE=test-loadgen
VOLUME_SIZE_GB=1
TOPO="${TOPO:-single}"
PLACEMENT_GROUP_NAME=test-placement
KEY_NAME=test-key
SG_NAME=test-sg
TAG_KEY=Project
TAG_VALUE=BlackBull-test
RUN_TAG_KEY=BlackBullRun
ROLE_TAG_KEY=Role
SERVER_ROLE_VALUE=server
LOADGEN_ROLE_VALUE=loadgen
SSH_USER=tester
SSH_OPTS=()
AMI_OWNER=test-owner
AMI_ARCH=x86_64
AMI_NAME_PATTERN=test-image
AWS_BASE=(aws)
_bench_aws_check_env() { return 0; }
_bench_aws_load_state() { source "$STATE_FILE"; }
'''


def run(script: Path, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("bash", str(script)),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_up_failure_after_instance_creation_runs_cleanup(tmp_path: Path) -> None:
    aws_dir = tmp_path / "bench" / "aws"
    bin_dir = tmp_path / "bin"
    aws_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "bench/aws/up.sh", aws_dir / "up.sh")
    (aws_dir / "config.sh").write_text(mock_config())
    write_executable(
        aws_dir / "down.sh",
        r'''#!/usr/bin/env bash
set -euo pipefail
: "${STATE_FILE:?}"
grep -q 'SERVER_INSTANCE_ID="i-server"' "$STATE_FILE"
printf 'cleanup i-server\n' >> "$MOCK_LOG"
rm -f "$STATE_FILE"
printf 'clean\n' > "${STATE_FILE}.clean"
''',
    )
    write_executable(
        bin_dir / "curl",
        "#!/usr/bin/env bash\nprintf '203.0.113.1\\n'\n",
    )
    write_executable(bin_dir / "ssh", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        bin_dir / "aws",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$MOCK_LOG"
case "$*" in
    *describe-images*) printf 'ami-test\n' ;;
    *'describe-key-pairs --key-names'*) exit 1 ;;
    *create-key-pair*) printf 'mock-key-material\n' ;;
    *'describe-security-groups --filters'*) printf 'None\n' ;;
    *'describe-security-groups --group-ids'*) : ;;
    *create-security-group*) printf 'sg-test\n' ;;
    *authorize-security-group-ingress*) : ;;
    *run-instances*) printf 'i-server\n' ;;
    *'wait instance-running'*) exit 9 ;;
    *) : ;;
esac
''',
    )
    state = aws_dir / "mock-state"
    log = tmp_path / "calls.log"
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
        "MOCK_LOG": str(log),
    }

    result = run(aws_dir / "up.sh", env=env)

    assert result.returncode != 0
    assert "cleanup i-server" in log.read_text()
    assert not state.exists()


def test_down_retains_state_when_verification_finds_resources(tmp_path: Path) -> None:
    aws_dir = tmp_path / "bench" / "aws"
    bin_dir = tmp_path / "bin"
    aws_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "bench/aws/down.sh", aws_dir / "down.sh")
    (aws_dir / "config.sh").write_text(mock_config())
    write_executable(bin_dir / "ssh", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        bin_dir / "aws",
        r'''#!/usr/bin/env bash
set -euo pipefail
case "$*" in
    *'describe-instances --filters'*) printf 'i-leftover\n' ;;
    *'describe-security-groups --filters'*) printf 'None\n' ;;
    *'describe-key-pairs --filters'*) printf 'None\n' ;;
    *'describe-placement-groups --filters'*) printf 'None\n' ;;
    *) : ;;
esac
''',
    )
    state = aws_dir / "mock-state"
    state.write_text(
        'TOPO="single"\nSG_ID="sg-test"\nAMI_ID="ami-test"\n'
        'PLACEMENT_GROUP_NAME="test-placement"\n'
        'SERVER_INSTANCE_ID="i-server"\nSERVER_PUBLIC_IP="203.0.113.1"\n'
        'SERVER_PRIVATE_IP="10.0.0.1"\nLOADGEN_INSTANCE_ID=""\n'
        'LOADGEN_PUBLIC_IP=""\nLOADGEN_PRIVATE_IP=""\n'
        'INSTANCE_ID="i-server"\nPUBLIC_IP="203.0.113.1"\n'
    )
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
    }

    result = run(aws_dir / "down.sh", env=env)

    assert result.returncode != 0
    assert state.exists()
    assert not Path(f"{state}.clean").exists()

    write_executable(
        bin_dir / "aws",
        "#!/usr/bin/env bash\nprintf 'None\\n'\n",
    )
    retry = run(aws_dir / "down.sh", env=env)
    assert retry.returncode == 0, retry.stderr
    assert not state.exists()
    assert Path(f"{state}.clean").read_text() == "verified\n"


def test_down_retains_state_when_proof_publication_fails(tmp_path: Path) -> None:
    aws_dir = tmp_path / "bench" / "aws"
    bin_dir = tmp_path / "bin"
    aws_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "bench/aws/down.sh", aws_dir / "down.sh")
    (aws_dir / "config.sh").write_text(mock_config())
    write_executable(bin_dir / "aws", "#!/usr/bin/env bash\nprintf 'None\\n'\n")
    write_executable(
        bin_dir / "mv",
        r'''#!/usr/bin/env bash
last="${!#}"
case "$last" in
    *.clean) exit 9 ;;
    *) exec /bin/mv "$@" ;;
esac
''',
    )
    state = aws_dir / "mock-state"
    state.write_text(
        'TOPO="single"\nSG_ID=""\nAMI_ID="ami-test"\n'
        'PLACEMENT_GROUP_NAME="test-placement"\n'
        'SERVER_INSTANCE_ID=""\nSERVER_PUBLIC_IP=""\n'
        'SERVER_PRIVATE_IP=""\nLOADGEN_INSTANCE_ID=""\n'
        'LOADGEN_PUBLIC_IP=""\nLOADGEN_PRIVATE_IP=""\n'
        'INSTANCE_ID=""\nPUBLIC_IP=""\n'
    )
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
    }

    result = run(aws_dir / "down.sh", env=env)

    assert result.returncode != 0
    assert state.exists()
    assert not Path(f"{state}.clean").exists()


def test_up_recovers_instance_when_run_command_loses_its_response(tmp_path: Path) -> None:
    aws_dir = tmp_path / "bench" / "aws"
    bin_dir = tmp_path / "bin"
    aws_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "bench/aws/up.sh", aws_dir / "up.sh")
    shutil.copy2(ROOT / "bench/aws/down.sh", aws_dir / "down.sh")
    (aws_dir / "config.sh").write_text(mock_config())
    write_executable(bin_dir / "curl", "#!/usr/bin/env bash\nprintf '203.0.113.1\\n'\n")
    write_executable(bin_dir / "ssh", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        bin_dir / "aws",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$MOCK_LOG"
case "$*" in
    *describe-images*) printf 'ami-test\n' ;;
    *'describe-key-pairs --key-names'*) exit 1 ;;
    *create-key-pair*) printf 'mock-key-material\n' ;;
    *'describe-security-groups --filters'*) printf 'None\n' ;;
    *'describe-security-groups --group-ids'*) : ;;
    *create-security-group*) printf 'sg-test\n' ;;
    *authorize-security-group-ingress*) : ;;
    *run-instances*)
        [[ "$*" =~ --client-token\ ([^[:space:]]+) ]] || exit 8
        client_token="${BASH_REMATCH[1]}"
        run_token="${client_token%-server}"
        case "$*" in
            *"Key=BlackBullRun,Value=$run_token"*) ;;
            *) exit 8 ;;
        esac
        printf '%s\n' "$run_token" > "$RUN_TOKEN_FILE"
        touch "$INSTANCE_CREATED"
        exit 9
        ;;
    *'describe-instances --filters'*)
        run_token="$(cat "$RUN_TOKEN_FILE" 2>/dev/null || true)"
        if [ -n "$run_token" ] && \
           [[ "$*" == *"Name=tag:BlackBullRun,Values=$run_token"* ]] && \
           [ -e "$INSTANCE_CREATED" ] && [ ! -e "$INSTANCE_TERMINATED" ]; then
            printf 'i-recovered\n'
        else
            printf 'None\n'
        fi
        ;;
    *terminate-instances*) touch "$INSTANCE_TERMINATED" ;;
    *'wait instance-terminated'*) : ;;
    *'describe-key-pairs --filters'*) printf 'None\n' ;;
    *'describe-placement-groups --filters'*) printf 'None\n' ;;
    *delete-security-group*|*delete-key-pair*) : ;;
    *) : ;;
esac
''',
    )
    state = aws_dir / "mock-state"
    log = tmp_path / "calls.log"
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
        "MOCK_LOG": str(log),
        "INSTANCE_CREATED": str(tmp_path / "created"),
        "INSTANCE_TERMINATED": str(tmp_path / "terminated"),
        "RUN_TOKEN_FILE": str(tmp_path / "run-token"),
    }

    result = run(aws_dir / "up.sh", env=env)

    assert result.returncode != 0
    assert "terminate-instances --instance-ids i-recovered" in log.read_text()
    assert not state.exists()
    assert Path(f"{state}.clean").read_text() == "verified\n"


def test_up_recovers_key_pair_when_create_response_is_lost(tmp_path: Path) -> None:
    aws_dir = tmp_path / "bench" / "aws"
    bin_dir = tmp_path / "bin"
    aws_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "bench/aws/up.sh", aws_dir / "up.sh")
    shutil.copy2(ROOT / "bench/aws/down.sh", aws_dir / "down.sh")
    (aws_dir / "config.sh").write_text(mock_config())
    write_executable(
        bin_dir / "aws",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$MOCK_LOG"
case "$*" in
    *describe-images*) printf 'ami-test\n' ;;
    *'describe-key-pairs --filters'*) printf 'None\n' ;;
    *create-key-pair*) touch "$KEY_CREATED"; exit 9 ;;
    *delete-key-pair*) touch "$KEY_DELETED" ;;
    *'describe-instances --filters'*) printf 'None\n' ;;
    *'describe-security-groups --filters'*) printf 'None\n' ;;
    *'describe-placement-groups --filters'*) printf 'None\n' ;;
    *) : ;;
esac
''',
    )
    state = aws_dir / "mock-state"
    log = tmp_path / "calls.log"
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
        "MOCK_LOG": str(log),
        "KEY_CREATED": str(tmp_path / "key-created"),
        "KEY_DELETED": str(tmp_path / "key-deleted"),
    }

    result = run(aws_dir / "up.sh", env=env)

    assert result.returncode != 0
    assert (tmp_path / "key-created").exists()
    assert (tmp_path / "key-deleted").exists()
    assert not state.exists()
    assert Path(f"{state}.clean").read_text() == "verified\n"


def test_up_recovers_security_group_when_create_response_is_lost(tmp_path: Path) -> None:
    aws_dir = tmp_path / "bench" / "aws"
    bin_dir = tmp_path / "bin"
    aws_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "bench/aws/up.sh", aws_dir / "up.sh")
    shutil.copy2(ROOT / "bench/aws/down.sh", aws_dir / "down.sh")
    (aws_dir / "config.sh").write_text(mock_config())
    (aws_dir / "mock-key").write_text("fixture")
    write_executable(bin_dir / "curl", "#!/usr/bin/env bash\nprintf '203.0.113.1\\n'\n")
    write_executable(
        bin_dir / "aws",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$MOCK_LOG"
case "$*" in
    *describe-images*) printf 'ami-test\n' ;;
    *'describe-key-pairs --filters'*) printf 'test-key\n' ;;
    *'describe-security-groups --filters'*)
        if [ -e "$SG_CREATED" ] && [ ! -e "$SG_DELETED" ]; then
            printf 'sg-recovered\n'
        else
            printf 'None\n'
        fi
        ;;
    *create-security-group*) touch "$SG_CREATED"; exit 9 ;;
    *delete-security-group*) touch "$SG_DELETED" ;;
    *'describe-instances --filters'*) printf 'None\n' ;;
    *'describe-placement-groups --filters'*) printf 'None\n' ;;
    *) : ;;
esac
''',
    )
    state = aws_dir / "mock-state"
    log = tmp_path / "calls.log"
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
        "MOCK_LOG": str(log),
        "SG_CREATED": str(tmp_path / "sg-created"),
        "SG_DELETED": str(tmp_path / "sg-deleted"),
    }

    result = run(aws_dir / "up.sh", env=env)

    assert result.returncode != 0
    assert (tmp_path / "sg-created").exists()
    assert (tmp_path / "sg-deleted").exists()
    assert not state.exists()
    assert Path(f"{state}.clean").read_text() == "verified\n"


def test_up_does_not_modify_reused_security_group(tmp_path: Path) -> None:
    aws_dir = tmp_path / "bench" / "aws"
    bin_dir = tmp_path / "bin"
    aws_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "bench/aws/up.sh", aws_dir / "up.sh")
    shutil.copy2(ROOT / "bench/aws/down.sh", aws_dir / "down.sh")
    (aws_dir / "config.sh").write_text(mock_config())
    (aws_dir / "mock-key").write_text("fixture")
    write_executable(bin_dir / "curl", "#!/usr/bin/env bash\nprintf '203.0.113.1\\n'\n")
    write_executable(
        bin_dir / "aws",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$MOCK_LOG"
case "$*" in
    *describe-images*) printf 'ami-test\n' ;;
    *'describe-key-pairs --filters'*) printf 'test-key\n' ;;
    *'describe-security-groups --filters'*) printf 'sg-existing\n' ;;
    *'describe-security-groups --group-ids'*)
        if [[ "$*" == *"IpProtocol=="* && "$*" == *"ToPort=="* ]]; then
            :
        else
            printf '203.0.113.1/32\n'
        fi
        ;;
    *'describe-instances --filters'*) printf 'None\n' ;;
    *run-instances*) exit 8 ;;
    *) : ;;
esac
''',
    )
    state = aws_dir / "mock-state"
    log = tmp_path / "calls.log"
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
        "MOCK_LOG": str(log),
    }

    result = run(aws_dir / "up.sh", env=env)

    calls = log.read_text()
    assert result.returncode != 0
    assert "authorize-security-group-ingress" not in calls
    assert "delete-security-group" not in calls
    assert "run-instances" not in calls


def test_up_rejects_reused_security_group_with_partial_benchmark_range(
    tmp_path: Path,
) -> None:
    aws_dir = tmp_path / "bench" / "aws"
    bin_dir = tmp_path / "bin"
    aws_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "bench/aws/up.sh", aws_dir / "up.sh")
    shutil.copy2(ROOT / "bench/aws/down.sh", aws_dir / "down.sh")
    (aws_dir / "config.sh").write_text(mock_config())
    (aws_dir / "mock-key").write_text("fixture")
    write_executable(bin_dir / "curl", "#!/usr/bin/env bash\nprintf '203.0.113.1\\n'\n")
    write_executable(
        bin_dir / "aws",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$MOCK_LOG"
case "$*" in
    *describe-images*) printf 'ami-test\n' ;;
    *'describe-key-pairs --filters'*) printf 'test-key\n' ;;
    *'describe-security-groups --filters'*) printf 'sg-existing\n' ;;
    *'describe-security-groups --group-ids'*IpRanges*) printf '203.0.113.1/32\n' ;;
    *'describe-security-groups --group-ids'*'FromPort==`22`'*) printf 'sg-existing\n' ;;
    *'describe-security-groups --group-ids'*'FromPort==`8000`'*)
        if [[ "$*" == *"IpProtocol=="* && "$*" == *"ToPort=="* ]]; then
            :
        else
            printf 'sg-existing\n'
        fi
        ;;
    *'describe-instances --filters'*) printf 'None\n' ;;
    *run-instances*) exit 8 ;;
    *) : ;;
esac
''',
    )
    state = aws_dir / "mock-state"
    log = tmp_path / "calls.log"
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
        "MOCK_LOG": str(log),
        "TOPO": "split",
    }

    result = run(aws_dir / "up.sh", env=env)

    calls = log.read_text()
    assert result.returncode != 0
    assert "authorize-security-group-ingress" not in calls
    assert "delete-security-group" not in calls
    assert "run-instances" not in calls


def test_httparena_wrapper_requires_teardown_proof(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    aws_dir = tmp_path / "bench" / "aws"
    meta_dir = tmp_path / "bench" / "httparena"
    scripts.mkdir()
    aws_dir.mkdir(parents=True)
    meta_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/run-httparena-bench.sh", scripts / "run-httparena-bench.sh")
    (meta_dir / "meta.json").write_text('{"tests":["fixture"]}\n')
    write_executable(aws_dir / "httparena_compare.sh", "#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "fixture"), check=True)
    subprocess.run(
        ("git", "-C", str(tmp_path), "config", "user.email", "fixture@example.invalid"),
        check=True,
    )
    subprocess.run(("git", "-C", str(tmp_path), "add", "."), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-qm", "fixture"), check=True)
    state = aws_dir / "mock-state"
    env = os.environ | {
        "APPROVE_CLOUD": "1",
        "STATE_FILE": str(state),
    }

    result = run(scripts / "run-httparena-bench.sh", env=env)

    assert result.returncode != 0
    assert not list((tmp_path / "bench/results/httparena").glob("*.complete.json"))


def test_httparena_driver_propagates_teardown_failure(tmp_path: Path) -> None:
    driver = (ROOT / "bench/aws/httparena_compare.sh").read_text()
    function = re.search(r"(?ms)^_teardown\(\) \{\n.*?^\}\n", driver)
    assert function is not None
    aws_dir = tmp_path / "bench" / "aws"
    aws_dir.mkdir(parents=True)
    write_executable(aws_dir / "down.sh", "#!/usr/bin/env bash\nexit 9\n")
    test_driver = aws_dir / "driver.sh"
    test_driver.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        "KEEP_INSTANCE=0\n"
        + function.group()
        + "trap _teardown EXIT\n"
        + "exit 0\n"
    )
    result = subprocess.run(
        ("bash", str(test_driver)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1


def test_ab_wrapper_requires_teardown_proof(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    aws_dir = tmp_path / "bench" / "aws"
    bin_dir = tmp_path / "bin"
    scripts.mkdir()
    aws_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(ROOT / "scripts/run-ab-verify.sh", scripts / "run-ab-verify.sh")
    (aws_dir / "config.sh").write_text(
        r'''#!/usr/bin/env bash
SSH_USER=tester
SSH_OPTS=()
_bench_aws_load_state() {
    SERVER_PUBLIC_IP=203.0.113.1
    LOADGEN_PUBLIC_IP=
}
'''
    )
    write_executable(
        aws_dir / "up.sh",
        '#!/usr/bin/env bash\nprintf \'SERVER_INSTANCE_ID="i-test"\\n\' > "$STATE_FILE"\n',
    )
    write_executable(aws_dir / "install.sh", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        aws_dir / "ab.sh",
        '#!/usr/bin/env bash\n[ "$1" != finish ] || rm -f "$STATE_FILE"\n',
    )
    write_executable(bin_dir / "ssh", "#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "fixture"), check=True)
    subprocess.run(
        ("git", "-C", str(tmp_path), "config", "user.email", "fixture@example.invalid"),
        check=True,
    )
    subprocess.run(("git", "-C", str(tmp_path), "add", "."), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-qm", "fixture"), check=True)
    state = aws_dir / "mock-state"
    env = os.environ | {
        "APPROVE_CLOUD": "1",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
    }

    result = run(scripts / "run-ab-verify.sh", env=env)

    assert result.returncode != 0
    assert not list((tmp_path / "bench/results").glob("*.complete.json"))
