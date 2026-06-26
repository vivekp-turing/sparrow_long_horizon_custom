import os
import re
import select
import subprocess
import time
from pathlib import Path

WORKDIR = Path(os.environ.get("TASK_PACKAGE_WORKDIR", "/app"))
GATEWAY_MODULE = "my-gateway"
RETRY_FILTER = WORKDIR / GATEWAY_MODULE / "src/main/java/com/leoli/gateway/filter/resilience/RetryGlobalFilter.java"


def run_cmd(cmd, timeout=180):
    return subprocess.run(cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=timeout)


def strip_java_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//.*", "", text)
    return text


def retry_filter_code():
    assert RETRY_FILTER.exists(), f"Expected retry filter implementation at {RETRY_FILTER.relative_to(WORKDIR)}"
    code = strip_java_comments(RETRY_FILTER.read_text())
    assert re.search(r"class\s+RetryGlobalFilter\b", code), "RetryGlobalFilter.java must define the RetryGlobalFilter class"
    return code


def test_gateway_project_compiles_without_databuffer_or_retry_filter_errors():
    result = run_cmd(["mvn", "--batch-mode", "-q", "-pl", GATEWAY_MODULE, "-am", "-DskipTests", "compile"], timeout=240)
    combined = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, combined[-4000:]
    assert "cannot find symbol" not in combined or "DataBuffer" not in combined, combined[-4000:]
    assert "RetryGlobalFilter" not in combined or "[ERROR]" not in combined, combined[-4000:]


def _package_gateway_jar():
    result = run_cmd(["mvn", "--batch-mode", "-q", "-pl", GATEWAY_MODULE, "-am", "-DskipTests", "package"], timeout=300)
    combined = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, combined[-4000:]
    target_dir = WORKDIR / GATEWAY_MODULE / "target"
    jars = [
        jar for jar in target_dir.glob("*.jar")
        if not any(marker in jar.name for marker in ("sources", "javadoc", "original"))
    ]
    assert len(jars) > 0, f"No runnable gateway jar was produced in {target_dir.relative_to(WORKDIR)}"
    return max(jars, key=lambda path: path.stat().st_mtime)


def test_gateway_application_starts_with_resolved_runtime_configuration():
    jar = _package_gateway_jar()
    cmd = [
        "java", "-jar", str(jar),
        "--server.port=0",
        "--management.server.port=0",
        "--spring.main.lazy-initialization=true",
        "--spring.cloud.nacos.config.enabled=false",
        "--spring.cloud.nacos.discovery.enabled=false",
        "--spring.cloud.nacos.config.namespace=public",
        "--spring.cloud.nacos.discovery.namespace=public",
        "--gateway.nacos.namespace=public",
        "--nacos.namespace=public",
        "--logging.level.root=INFO",
    ]
    proc = subprocess.Popen(cmd, cwd=WORKDIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log_lines = []
    started = False
    forbidden_fragments = [
        "BindException",
        "PortInUseException",
        "Web server failed to start",
        "Could not resolve placeholder",
        "IllegalArgumentException: namespace",
        "No namespace",
    ]
    try:
        deadline = time.time() + 75
        while time.time() < deadline:
            if proc.poll() is not None:
                remaining = proc.stdout.read() if proc.stdout else ""
                if remaining:
                    log_lines.append(remaining)
                break
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if line:
                    log_lines.append(line)
                    if "Started " in line or "Netty started on port" in line or "Tomcat started on port" in line:
                        started = True
                        break
        combined = "".join(log_lines)
        for fragment in forbidden_fragments:
            assert fragment not in combined, combined[-4000:]
        assert started, "Gateway did not reach a successful started state. Recent log output:\n" + combined[-4000:]
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_retry_filter_marks_http_500_backend_responses_as_retryable():
    code = retry_filter_code()
    has_retry_operator = re.search(r"\.retryWhen\s*\(|\bRetry\.(?:backoff|fixedDelay|max|from)\s*\(", code)
    has_status_inspection = re.search(r"\bgetStatusCode\s*\(|\bstatusCode\s*\(\)|\bsetStatusCode\s*\(", code)
    has_http_500_or_5xx_predicate = re.search(
        r"\b500\b|INTERNAL_SERVER_ERROR|is5xxServerError\s*\(|value\s*\(\)\s*>=\s*500|series\s*\(\)\s*==\s*HttpStatus\.Series\.SERVER_ERROR",
        code,
    )
    assert has_retry_operator, "RetryGlobalFilter must use Reactor retry behavior rather than passing backend failures through once"
    assert has_status_inspection, "RetryGlobalFilter must inspect backend response status codes"
    assert has_http_500_or_5xx_predicate, "HTTP 500 or 5xx backend responses must be classified as retryable"


def test_retry_filter_does_not_retry_success_or_unconfigured_statuses_unconditionally():
    code = retry_filter_code()
    retry_positions = [match.start() for match in re.finditer(r"\.retryWhen\s*\(", code)]
    assert len(retry_positions) > 0, "RetryGlobalFilter must contain an actual retryWhen operation"
    windows = [code[max(0, pos - 600):pos + 600] for pos in retry_positions]
    assert len(windows) > 0, "Expected retryWhen context windows to be available for validation"
    guarded_retry = any(
        re.search(r"\.filter\s*\(|(?:retryable|retryStatus|statuses|statusCodes|statusList)\s*\.contains\s*\(", window, re.I)
        for window in windows
    )
    conditional_status_error = re.search(
        r"if\s*\([^)]*(?:is5xxServerError|>=\s*500|INTERNAL_SERVER_ERROR|contains\s*\([^)]*status)[^)]*\)[\s\S]{0,300}(?:Mono\.error|throw\s+new|setStatusCode)",
        code,
    )
    non_retry_guard = re.search(
        r"is2xxSuccessful\s*\(|<\s*500|!\s*[^;\n]*is5xxServerError|(?:retryable|retryStatus|statuses|statusCodes|statusList)\s*\.contains\s*\(",
        code,
        re.I,
    )
    assert guarded_retry or conditional_status_error, "Retry behavior must be guarded by retryable status/config checks, not applied to every response"
    assert non_retry_guard, "Successful or non-configured response statuses must have a path that avoids retry"
    unconditional_error = re.search(r"return\s+Mono\.error\s*\(\s*new\s+RuntimeException\s*\([^)]*\)\s*;\s*[\s\S]{0,120}\.retryWhen", code)
    assert not unconditional_error, "The filter appears to convert all responses into retryable errors"
