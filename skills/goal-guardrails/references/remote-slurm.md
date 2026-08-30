# Remote Slurm submission

Use `ssh-helper-v1` when the guarded project and controller are local but `sbatch` exists only on a frozen SSH host. This is one of the few workflows that still uses a lease in fast profile because duplicate submission is externally expensive. Fast admission performs deterministic validation without a separate reviewer; strict admission also requires its reviewer attestation. The local controller remains the sole owner of the lease, lock, runtime binding, and `optimization/CONTROL.json`. The remote host owns only a restricted helper and immutable submission receipts.

## Trust boundary

Do not run a second controller remotely, share `CONTROL.json` between hosts, or approve a raw `ssh host sbatch ...` Bash policy. OpenSSH passes a remote command to the account's shell, so an argv-looking SSH command is not an end-to-end exact-argv boundary. This transport sends a versioned JSON envelope on stdin to one safe absolute helper path. The helper invokes a digest-checked `sbatch` with a Python argv list and no shell.

For a stronger host-side boundary, dedicate an SSH key or account and configure an administrator-owned forced command for the helper. The plugin still freezes and verifies the local SSH executable, known-hosts file, helper digest, remote `sbatch`, project inputs, and receipt contract.

## Prepare the remote host

Copy the bundled `hooks/remote_submit_helper.py` to a fixed absolute path on the Slurm host, mark it executable, and create a private receipt directory. Deployment is an administrative setup step, not an admitted experiment mutation. Do not let each model project invent a wrapper.

Record SHA-256 values for:

- the local SSH executable;
- the dedicated known-hosts file;
- the bundled and deployed helper, which must be byte-identical;
- every submitted project input such as the Slurm script.

The frozen remote work directory must contain the same input bytes. `doctor` checks the deployed helper, remote `sbatch`, work directory, receipt root, and every remote input before submission. Submission repeats the relevant checks and pins the `sbatch` digest returned by doctor.

## Proposal contract

The transport is allowed only on a one-shot policy that captures a required runtime binding. All arguments remain in the frozen policy; `submit-bind` accepts no runtime argv. The example includes the `review` block for strict compatibility; fast profile ignores it and synthesizes controller validation.

```json
{
  "runtime_bindings": [
    {
      "id": "slurm-job",
      "kind": "slurm_job_id",
      "source_policy_id": "submit-slurm",
      "required": true
    }
  ],
  "bash_policies": [
    {
      "id": "submit-slurm",
      "phase": "workload",
      "executable": "/usr/bin/sbatch",
      "argv": [
        {"literal": "--parsable"},
        {"literal": "train.sbatch"}
      ],
      "cwd": ".",
      "output_paths": [],
      "resources": {"gpu": 0},
      "capture_binding": "slurm-job",
      "max_uses": 1,
      "timeout_seconds": 120,
      "transport": {
        "kind": "ssh-helper-v1",
        "ssh_executable": "/usr/bin/ssh",
        "ssh_executable_sha256": "<LOCAL_SSH_SHA256>",
        "host": "hpc142",
        "user": "USER",
        "port": 22,
        "known_hosts_file": "/absolute/path/to/dedicated_known_hosts",
        "known_hosts_sha256": "<KNOWN_HOSTS_SHA256>",
        "identity_file": "/absolute/path/to/dedicated_key",
        "identity_file_sha256": "<IDENTITY_FILE_SHA256>",
        "helper_path": "/opt/goal-guardrails/remote_submit_helper.py",
        "helper_sha256": "<BUNDLED_HELPER_SHA256>",
        "sbatch_path": "/usr/bin/sbatch",
        "remote_workdir": "/shared/project",
        "receipt_root": "/home/USER/.cache/goal-guardrails/submissions",
        "remote_files": [
          {"path": "train.sbatch", "sha256": "<TRAIN_SCRIPT_SHA256>"}
        ],
        "timeout_seconds": 30
      }
    }
  ],
  "review": {
    "decision": "ALLOW",
    "reviewer": "subagent:<id>",
    "reason": "remote identity, inputs, one-shot receipt, and recovery are bounded",
    "checks": {
      "evidence_sufficient": true,
      "lease_mutations_bounded": true,
      "pre_run_gates_sufficient": true,
      "mutation_not_required_before_admission": true,
      "remote_submission_contract_bounded": true
    }
  }
}
```

The proposal's `executable` represents the remote executable identity and must agree with `transport.sbatch_path`. Its basename must be `sbatch`. `remote_files.path` values are project-relative locally and relative to `remote_workdir` remotely.

## Run and recover

After deterministic fast admission, or fresh review plus strict admission:

```bash
python3 <plugin-root>/hooks/goal_guard.py doctor --policy submit-slurm --project .
python3 <plugin-root>/hooks/goal_guard.py submit-bind --policy submit-slurm --project .
```

The controller reserves the runtime binding, consumes the policy, and charges its one mutation before contacting SSH. The helper creates a write-once nonce receipt in `RUNNING` before invoking `sbatch`. A successful response binds the returned Job ID. A definitive nonzero `sbatch` result records `FAILED` and requires a fresh proposal.

If SSH times out, disconnects, emits malformed data, or cannot prove a terminal receipt, the controller records `UNCERTAIN`. Do not call `submit-bind` again. Reconcile the same nonce:

```bash
python3 <plugin-root>/hooks/goal_guard.py reconcile-bind --policy submit-slurm --project .
```

`reconcile-bind` only reads the immutable remote receipt. `SUCCEEDED` binds the existing Job ID; `FAILED`, `RUNNING`, `ABSENT`, or another transport failure never creates a second job. Follow `status.next_action` until the result becomes definitive or an operator decision is genuinely required.

The lease exposes `budget_plan`: one-shot submission costs one mutation; doctor, reconciliation, wait/wake, and controller receipt bookkeeping cost zero. Account external scheduler compute separately in the experiment budget.

After the binding succeeds, start the frozen deterministic monitor with the bound Job ID and use `wait-monitor` / `wake-monitor`. Strict mode additionally requires review. The monitor remains project-read-only and does not make Goal or business-metric decisions.
