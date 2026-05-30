import json
import os
import signal
import textwrap
import time
import urllib.request

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    EvalPolicy,
    GetBenchmarkRequest,
    StartRunRequest,
    StartTrialRequest,
    StatusRequest,
    SubmitRunRequest,
    TRIAL_STATE_DONE,
)
from connectrpc.errors import ConnectError

from agent import run_agent


BITGN_URL = (
    os.getenv("BITGN_HOST")
    or os.getenv("BENCHMARK_HOST")
    or "https://api.bitgn.com"
)
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
BENCH_ID = os.getenv("BENCH_ID") or os.getenv("BENCHMARK_ID") or "bitgn/ecom1-dev"
MODEL_ID = os.getenv("MODEL_ID") or "gpt-4.1-2025-04-14"
RUN_NAME = os.getenv("RUN_NAME") or "the-very-deterministic-clerk by @alexey_rybolovlev"
CHECK_STATUS = os.getenv("CHECK_STATUS", "0").lower() in {"1", "true", "yes"}
GET_BENCHMARK = os.getenv("GET_BENCHMARK", "1").lower() in {"1", "true", "yes"}
USE_TASK_INDEX = os.getenv("USE_TASK_INDEX", "0").lower() in {"1", "true", "yes"}
HARNESS_RPC_TIMEOUT_MS = int(os.getenv("HARNESS_RPC_TIMEOUT_MS", "60000"))

ECOM_DEV_TASK_ORDER = (
    "t01", "t02", "t03", "t04", "t05", "t06", "t07", "t08", "t09", "t10",
    "t11", "t12", "t13", "t14", "t15", "t16", "t17", "t18", "t19", "t20",
    "t21", "t22", "t23", "t24", "t25", "t26", "t27", "t28", "t29", "t30",
    "t31", "t32", "t33", "t34", "t35", "t36", "t37", "t38", "t39", "t40",
    "t41", "t42", "t43", "t44", "t45", "t46", "t47", "t48", "t49", "t50",
    "t51", "t52", "t53",
)
ECOM_PROD_TASK_ORDER = tuple(f"t{index:02d}" for index in range(1, 101))
DEFAULT_TASK_ORDERS = {
    "bitgn/ecom1-dev": ECOM_DEV_TASK_ORDER,
    "bitgn/ecom1-prod": ECOM_PROD_TASK_ORDER,
}

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"


class RpcTimeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise RpcTimeout("harness RPC timed out")


def call_harness(label: str, fn, attempts: int = 3, timeout_s: int = 60):
    last = None
    for attempt in range(1, attempts + 1):
        previous = signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, timeout_s)
        try:
            return fn()
        except (ConnectError, OSError, RpcTimeout) as exc:
            last = exc
            signal.setitimer(signal.ITIMER_REAL, 0)
            if attempt >= attempts:
                break
            delay = 2 * attempt
            print(
                f"{CLI_YELLOW}{label} failed: {type(exc).__name__}: {exc}; "
                f"retry in {delay}s ({attempt}/{attempts}){CLI_CLR}",
                flush=True,
            )
            time.sleep(delay)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    raise last


def submit_run_json(run_id: str) -> dict:
    """Call SubmitRun through Connect JSON so new batch-score fields are visible
    even when the installed generated protobuf package is older than the server."""
    url = f"{BITGN_URL.rstrip('/')}/bitgn.harness.HarnessService/SubmitRun"
    payload = json.dumps({"runId": run_id, "force": True}).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "content-type": "application/json",
            "connect-protocol-version": "1",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HARNESS_RPC_TIMEOUT_MS / 1000.0) as response:
        return json.loads(response.read().decode())


def _json_get(row: dict, snake: str, camel: str, default=None):
    if camel in row:
        return row[camel]
    return row.get(snake, default)


def _trial_done(state) -> bool:
    return state == TRIAL_STATE_DONE or str(state) in {"TRIAL_STATE_DONE", "DONE", "3"}


def filtered_trial_ids(
    task_filter: list[str],
    trial_ids: list[str],
    *,
    benchmark_tasks=None,
    bench_id: str = BENCH_ID,
) -> tuple[list[str], str]:
    if not task_filter:
        return trial_ids, "all"

    task_order = None
    source = ""
    if benchmark_tasks and len(benchmark_tasks) == len(trial_ids):
        task_order = [task.task_id for task in benchmark_tasks]
        source = "benchmark index"
    elif bench_id in DEFAULT_TASK_ORDERS and len(DEFAULT_TASK_ORDERS[bench_id]) == len(trial_ids):
        task_order = DEFAULT_TASK_ORDERS[bench_id]
        source = "built-in task order"

    if not task_order:
        return trial_ids, "scan"

    wanted = set(task_filter)
    return [
        trial_id
        for task_id, trial_id in zip(task_order, trial_ids)
        if task_id in wanted
    ], source


def main() -> None:
    task_filter = os.sys.argv[1:]
    scores = []
    score_by_task = {}
    run_complete = False
    run_failed = False
    benchmark_tasks = None

    try:
        client = HarnessServiceClientSync(BITGN_URL, timeout_ms=HARNESS_RPC_TIMEOUT_MS)
        if CHECK_STATUS:
            print(
                "Connecting to BitGN",
                call_harness(
                    "status",
                    lambda: client.status(
                        StatusRequest(),
                        timeout_ms=HARNESS_RPC_TIMEOUT_MS,
                    ),
                ),
            )
        if GET_BENCHMARK or (task_filter and USE_TASK_INDEX):
            res = call_harness(
                "get_benchmark",
                lambda: client.get_benchmark(
                    GetBenchmarkRequest(benchmark_id=BENCH_ID),
                    timeout_ms=HARNESS_RPC_TIMEOUT_MS,
                ),
            )
            benchmark_tasks = list(res.tasks)
            if GET_BENCHMARK:
                print(
                    f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
                    f"with {len(res.tasks)} tasks.\n{CLI_GREEN}{res.description}{CLI_CLR}"
                )
            else:
                print(f"Benchmark task index loaded: {BENCH_ID} ({len(res.tasks)} tasks)")
        else:
            print(f"Benchmark metadata skipped: {BENCH_ID}")

        run = call_harness(
            "start_run",
            lambda: client.start_run(
                StartRunRequest(
                    name=RUN_NAME,
                    benchmark_id=BENCH_ID,
                    api_key=BITGN_API_KEY,
                ),
                timeout_ms=HARNESS_RPC_TIMEOUT_MS,
            ),
        )

        try:
            trial_ids, filter_source = filtered_trial_ids(
                task_filter,
                list(run.trial_ids),
                benchmark_tasks=benchmark_tasks,
            )
            if task_filter and filter_source != "scan":
                print(f"Task filter resolved via {filter_source}: {', '.join(task_filter)}")

            for trial_id in trial_ids:
                trial = call_harness(
                    "start_trial",
                    lambda trial_id=trial_id: client.start_trial(
                        StartTrialRequest(trial_id=trial_id),
                        timeout_ms=HARNESS_RPC_TIMEOUT_MS,
                    ),
                )
                if task_filter and trial.task_id not in task_filter:
                    continue

                print(f"{'=' * 30} Starting task: {trial.task_id} {'=' * 30}")
                print(f"{CLI_BLUE}{trial.instruction}{CLI_CLR}\n{'-' * 80}")
                try:
                    run_agent(MODEL_ID, trial.harness_url, trial.instruction)
                except Exception as exc:
                    print(exc)

                result = call_harness(
                    "end_trial",
                    lambda trial=trial: client.end_trial(
                        EndTrialRequest(trial_id=trial.trial_id),
                        timeout_ms=HARNESS_RPC_TIMEOUT_MS,
                    ),
                )
                if result.score_available:
                    scores.append((trial.task_id, result.score))
                    style = CLI_GREEN if result.score == 1 else CLI_RED
                    explain = textwrap.indent("\n".join(result.score_detail), "  ")
                    score_by_task[trial.task_id] = result.score
                    print(
                        f"\n{style}Score: {result.score:0.2f}\n{explain}\n{CLI_CLR}"
                    )
                else:
                    print(f"\n{CLI_BLUE}Score: not available{CLI_CLR}\n")
            run_complete = True
        finally:
            print(f"\n{CLI_GREEN}>>>> Submitting run... <<<<{CLI_CLR}")
            result = call_harness(
                "submit_run",
                lambda: submit_run_json(run.run_id),
            )
            score_available = _json_get(result, "score_available", "scoreAvailable", False)
            if score_available:
                final_score = _json_get(result, "score", "score", 0.0)
                print(f"FINAL SCORE: {final_score:0.2f}")
                scores.clear()
                score_by_task.clear()
                incomplete = 0
                wanted = set(task_filter)
                for trial_result in result.get("trials", []):
                    task_id = _json_get(trial_result, "task_id", "taskId", "")
                    if wanted and task_id not in wanted:
                        continue
                    if not _trial_done(_json_get(trial_result, "state", "state")):
                        incomplete += 1
                        continue
                    score = float(_json_get(trial_result, "score", "score", 0.0))
                    score_detail = _json_get(trial_result, "score_detail", "scoreDetail", [])
                    scores.append((task_id, score))
                    score_by_task[task_id] = score
                    style = CLI_GREEN if score == 1 else CLI_RED
                    explain = textwrap.indent("\n".join(score_detail), "  ")
                    print(
                        f"- {task_id}: {style}Score: {score:0.2f}{CLI_CLR}\n"
                        f"{explain}"
                    )
                if incomplete:
                    print(f"{CLI_RED}incomplete trials: {incomplete}{CLI_CLR}")
            else:
                print(
                    f"\n{CLI_RED}Score is not available. "
                    f"Results are sealed and will be revealed later{CLI_CLR}\n"
                )

    except (ConnectError, OSError, RpcTimeout) as exc:
        run_failed = True
        if isinstance(exc, ConnectError):
            print(f"{exc.code}: {exc.message}")
        else:
            print(f"{type(exc).__name__}: {exc}")
    except KeyboardInterrupt:
        run_failed = True
        print(f"{CLI_RED}Interrupted{CLI_CLR}")

    if scores:
        for task_id, score in scores:
            style = CLI_GREEN if score == 1 else CLI_RED
            print(f"{task_id}: {style}{score:0.2f}{CLI_CLR}")

        missing = []
        if task_filter:
            missing = [task_id for task_id in task_filter if task_id not in score_by_task]
        if run_complete and not run_failed and not missing:
            total = sum(score for _, score in scores) / len(scores) * 100.0
            print(f"FINAL: {total:0.2f}%")
        else:
            partial = sum(score for _, score in scores) / len(scores) * 100.0
            print(f"INCOMPLETE: partial scored average {partial:0.2f}%")
            if missing:
                print(f"Missing scores: {', '.join(missing)}")


if __name__ == "__main__":
    main()
