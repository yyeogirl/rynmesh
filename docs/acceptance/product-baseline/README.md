# Product development baseline — 2026-09-10

Baseline: upstream `094f03ac673b84e571052c2dda1910b67e7b98e8`.
Environment: Windows, Python 3.12.10, existing development dependency environment.

Before product implementation, `python -m pytest tests -q --tb=short` completed:
**937 passed, 4 failed, 29 skipped**, 148.19 seconds. These are reproduced baseline
results, not accepted exclusions and not a claim that the branch is ready.

Failures to resolve or verify against the applicable platform contract:

1. `test_document_extract.py::test_child_extracts_plain_text` — returned CRLF differs from expected LF.
2. `test_llm_runtime_native.py::test_the_runtime_fetch_installs_the_https_only_redirect_handler` — archive fixture does not yield a Windows inference executable.
3. `test_mailbox.py::test_file_mailbox_store_deposit_poll_ack_and_caps` — POSIX mode assertion on Windows filesystem.
4. `test_mailbox.py::test_acked_message_leaves_a_tombstone_until_it_expires` — same mode assertion for tombstones.

No failing assertion was lowered or test skipped as part of this inventory.
Windows ACL guarantees need separate evidence; a POSIX mode-bit assertion is not that evidence.

After importing the original first-success commit (before the adaptation), the
frontend suite completed **55 passed / 11 files**. It is a reuse checkpoint,
not a clean-main frontend baseline and not final product acceptance.
