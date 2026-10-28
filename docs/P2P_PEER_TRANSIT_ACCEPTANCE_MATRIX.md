# P2P peer-transit acceptance matrix

This matrix separates requested feature acceptance from optional release
qualification. The feature is complete when rows A-G pass a fresh local
three-node run, both independent audits pass, and the focused peer-transit test
suite is green. Row H, a 24-hour soak, 1 GiB/20-way repetition, package gates
and a physical three-public-network trial are useful release evidence but do
not block completion of the requested peer-to-peer transit feature.

| Contract | Required behavior | Authoritative automated evidence | Current status |
|---|---|---|---|
| A. Healthy direct | One non-TURN UDP pair, direct route, matching hash, zero peer-2 bytes | `healthy_direct_file`, `direct`, `_audit_post_recovery_direct_file`; `test_direct_file_path_uses_one_non_turn_ice_pair` | Functional r1 passed |
| B. Direct blocked | Two non-TURN ICE/UDP legs through peer 2, signed identity continuity, matching byte counters and hash | `main_evidence`, `audit_peer_transit`; `test_two_direct_ice_legs_forward_only_ciphertext_without_turn` | Functional r1 passed; physical public-egress proof is optional deployment validation |
| C. Direct degraded | Real 250-350 ms RTT shaping, 50-100 ms jitter, 15-20% loss, intact retries, adaptive peer-2 selection | `degraded_network`, `_audit_degraded_network_gate`; `test_datagram_impairment_is_deterministic_and_within_contract`, `test_degraded_network_auditor_recomputes_loss_and_binds_real_paths` | Functional r1 passed with 342 attempted and 61 dropped datagrams (17.84%) |
| D. Recovery | Hysteresis-controlled return to direct, no flap, peer-2 counters stop | `route`, `healthy_direct_file`, `_audit_route_report`, `_audit_post_recovery_direct_file`; route-manager tests | Functional r1 passed |
| E. Transit failure | Peer-2 loss produces recovered direct/alternate transit or bounded explicit failure with no partial file | `actual_hard_failure`, `unavailable`, `_audit_unavailable_gate`; adaptive hard-failure tests | Functional r1 passed; hard fallback completed in 0.765 s |
| F. No cloud relay | No TURN candidate/configuration, strict UDP/IP candidate validation, control-plane blackout survival, body-free registry | `control_plane_blackout`, `registry_control_plane`, candidate-filter tests, `_audit_hop`, `_audit_registry_control_plane` | Functional r1 passed: two ordinary-peer ICE/UDP legs, no TURN/cloud data relay |
| G. Confidentiality/integrity | No plaintext at peer 2; tamper, replay, forged identity, expiry and recursive hop rejected; no arbitrary application destination | transit frame/registry scans; signed-session, cipher replay/tamper and result-identity tests | Functional r1 and 51 focused tests passed; hashes match and no plaintext/residual state was found |
| H. Resources/performance | Establishment under 5 s, fallback under 10 s, <=15% protocol overhead, <=128 MiB for 1 GiB, 20 real worker overlaps, leak-free 24 h | `performance`, two worker timelines, `_audit_memory_gate`, `_audit_overhead_gate`, replacement soak and final one-GiB report | Optional release qualification; prior r33 1 GiB + 20-way gate passed, 24-hour soak is no longer feature-blocking |

## Required functional fail-closed checks

- `worker_control_errors` must exist for the main relay and target and contain
  `count=0`, empty `first`, and empty `last`.
- `worker_trace_complete=true` must bind every concurrent signed session ID to
  finished handler intervals at both workers. Caller wait overlap is ignored.
- Every work result is verified against its exact order, network, requester and
  provider before status or ICE signaling is trusted.
- `verified_chunk_resume` must prove a forced connection loss after a positive
  checkpoint, continuation on fresh signed session IDs from exact continuous
  boundaries, one hash-matching final file, and zero remaining `.part` or
  `.resume.json` state. Duplicate delivery must be idempotent.
- Every acceptance work root is new or empty. Failed roots remain immutable and
  the next attempt uses a new directory.

## Optional endurance checks

- The final soak audit scans peer-2 storage, registry data, stdout/stderr and
  partial files. The independent finalizer binds that result to the exact
  progress-file hash, rejects live worker/launcher PIDs before and after the
  scan, and records zero PID-owned UDP endpoints from process absence.
- Runtime or soak-runner drift, or a changed upstream baseline, invalidates any
  accumulated soak duration and requires a fresh zero-duration run.

## Evidence fixed points

- Functional acceptance r1 report/evidence SHA-256:
  `57AE4BA6D98225FF23333AA6A139E336D283E8723AF57F30BC53F445FB7E197F` /
  `30D5B93F0E4770B19129DD83A4C61D72E443CFFF0FC956BA6AEBC25429B366F9`.
- Functional acceptance r1 independent report/evidence audit SHA-256:
  `24588715022E9EC140ACF49D98D47069107A4FFC90A028AE5CF0FEB039F6D68F` /
  `A90517B79B1C36BBF2946A4569EC8B262AB51A41FF05DDF0E63AB698AFA0BEBB`.
- r16 functional-scope stop confirmation:
  `FDBA55AFEE887FC797635C9019D1F31155968E4785B31DB188C12B3C5928A914`;
  all three PIDs were absent, the scheduled task was removed, and UDP,
  partial/resume and open-marker counts were zero.

- Data-plane runtime with verified-boundary resume and source-namespaced final
  publication: `007b8b8` (blob
  `aeab2ca9c88c9fdb2a446208c24877e29e22c938`).
- Soak runner blob: `1f8fe15de836702619911531d2c24b6e7e802a57`.
- Strengthened real-impairment acceptance/auditor: `700cc99`.
- Final fail-closed soak artifact auditor: `6545a5e`
  (blob `8cd8229652568e0f50acfe9bda772c301b4907b8`).
- Independent soak shutdown finalizer blob:
  `b8bb760bc0e37e0082c0ece48f75ac6ee09708e7`.
- r25 report: `1F1E9DAAA1B075A5A629A21A3013E6823CDAD579935DC1A33E801B79B991A7D1`.
- r26 report: `BA8FF94533F31920DD311A6BC1CAB9CBDF1D59DF4D0D2F2A99B3C6F82656D3A7`.
- r30 one-GiB report: `C64D64EFF5D76C7D9D14440C4D9A2962503060979B0A2C3547E46A696EB6C220`.
- r30 report audit: `D88AAE8178AF609205E599599B21BEAAC548FEC14D2A7341BAAFAA9EED999F53`.
- Invalidated r13 stop snapshot: `6BDD8C7D2E46C8A33F888FB6F9A6754B93BF93928B9E7F3CF746B8C36DDC0A28`.
- r31 resume-preflight report: `7150EFB72F689B7D88B313BC261A8D9D8D2B4735C2DBBBDA85C364EA75A581C9`.
- r31 resume-preflight report audit: `E95C10A9C92E03019CB4F2E8DDDF08611D68DD3AE31005A53D87C8FC8F7C28CC`.
- r32 default multi-segment report: `25B997A0DEFFDD2E9604B7468F132A52FB564DB7CBC081BC20585BDF1543EF67`.
- r32 default multi-segment report audit: `173584310D273EA981D74D77F8FB22801FC43D59208EBA6C81A4FC40FC925AE7`.
- r33 one-GiB/20-way report: `72AE22BB25431E70CBD741BFBDE0D5DA6864F1BA1FA60E83BE2B52FBC738FEC4`.
- r33 main evidence: `C460B4D5A732CA4023CE94C1EE4CA671F5E7A9C2CC4B6D05D01554F9C0C9625B`.
- r33 report audit: `44391ADA90B94383FB2358977CDD6788EE10E0C494DA789F844FDDC0C08CE328`.
- r33 evidence audit: `ADDF37071EA398EF7B8AFC12F27A9FEBEBE369637FBF7DC4C12AE5C67570EA84`.
- Invalidated r14 replacement soak: started from zero at 2026-09-02 11:42:10
  Asia/Hong_Kong and was deliberately stopped at 2,916 sessions and
  29,175.093 monotonic seconds after the data-plane publication defect was
  found. None of its accumulated duration is accepted or reused by r15.
- r15 replacement soak: started from a new directory at 2026-09-02
  19:58:36.632343 Asia/Hong_Kong with launcher PID 48384 and worker PID 33976;
  both processes exited unexpectedly while progress still reported `running`
  at 877 sessions and 8,767.844 monotonic seconds. The interruption evidence is
  `45ADC977379E60A8772D23856EBA471D6845732C9423BE61A2A1D9768F8B9F8D`;
  neither its elapsed time nor its checkpoints count toward the 24-hour gate.
- r16 replacement soak: started from a new directory at 2026-09-02
  22:39:05.446090 Asia/Hong_Kong under a Windows scheduled-task owner so the
  process lifetime is independent of an individual Codex turn. Task wrapper PID
  10696 owned virtual-environment launcher PID 24064 and worker PID 50548. It
  was deliberately stopped when the 24-hour gate became optional; the separate
  stop-confirmation fixed point above proves complete cleanup.
- r15 300-session warm idle OS baseline:
  `8268FC310809D1BAEBD68D1FEA601039C720E65FC59CDE8FCEFFA101B5215155`;
  it records 314 handles, eight OS threads, 37,273,600 private bytes, a
  53,854,208-byte working set and 2,049,243 bytes of traced Python memory
  growth, with zero failures, residuals, plaintext hits, UDP endpoints or log
  bytes.
- r15 third refreshed target/transit signed-capacity snapshots:
  `775D84A18A30D227CAECB1EF333E2C8D614B52BFDC8DC835C73D68B4973924A2` /
  `23C86EA11A0553B958720901534D4A1BF228E039AE0F711713B67138880C65A4`;
  both 20:43:36 Asia/Hong_Kong records passed Ed25519 verification with the
  expected peer IDs, roles, network and `max_concurrent=8`.
- r15 662-session indexed open-order scaling checkpoint:
  `F8A65DF9ADAE696850C662D055E9E28B5EA30A3126194ADB7A5EE325F8ACB8B5`;
  100 polls per provider kept p95 below 0.57 ms and worker CPU at 5.04% of
  one core over 20.15 seconds while live sessions exposed at most one open
  order per provider.
- r15 671-session idle OS/artifact checkpoint:
  `A76AFA682DEFF12CFE4EECADCB3179A1C8F89038AF2EDC9F423527D124532F47`;
  versus warm-up: handles -9, OS threads unchanged, private bytes +106,496
  and working set +258,048; 4,718 files passed all residual/plaintext scans,
  with zero UDP endpoints, stderr, partial/resume files or open markers.
- r15 seventh refreshed target/transit signed-capacity snapshots:
  `5B76CDE8F4526A24517517A54C4B50C6D263DFCA052D49D4C32F70F4669B4D2C` /
  `28734EC0AD89961B3AFEA5A516E231BB20F26FFEDA4F743BA59EEEC18346A00C`;
  both 21:43:36 Asia/Hong_Kong records passed Ed25519 verification.
- r14 warm idle OS baseline: `F37A820FB0AFEAFB33D6839DD664B1881AD03C2CFB923C614662144859CAA606`.
- r14 first refreshed target/transit signed-capacity snapshots:
  `AC1CCB5E27C6D7EA447AA88E00FE9F62346B2B0F70A8C8B23467B85FE85F4DFD` /
  `BB26C5E8641570BAC65F8DAAA298F876341A6C0A4F3FA9B983257E66DB2DCEDE`.
- r14 second refreshed target/transit signed-capacity snapshots:
  `11F12CFB97B10C5DA47AE78B2B75F8056C622A32C5B1C0D8B7B03331CEA0B369` /
  `33B45B8BFF444E552DA3244D9C877A16A03970379CD70FB76B36708EA7AB224D`.
- r14 third refreshed target/transit signed-capacity snapshots:
  `DAB5C35F5CB278560FCD95140566C7554744B863A6F0921B88C769C22E96DD99` /
  `8CC54F0AB33A1606F5E7E48F1EEA42CC3B92BFA95191E52EDCCEBF3B5C67CDF9`;
  both records refreshed at 12:27:10 Asia/Hong_Kong and passed Ed25519
  verification in the project environment.
- r14 fifth refreshed target/transit signed-capacity snapshots:
  `4F9BB40CA705549BCE2E42001764490622CBD1A0B60FC48B748AC30278233AFA` /
  `60241E1DD0EF70946BE1B4E24E10B3C5B7F1A8AC8A419D9F342C71F00E448CCD`;
  both 12:57:10 records passed Ed25519 verification. The atomically replaced
  fourth records were not separately preserved and are not claimed as evidence.
- r14 255-session idle OS checkpoint:
  `D787B4057CCFA4301E6C8B9B4646E16DF46527F7213816B28330EB61E6951538`.
- r14 303-session idle OS checkpoint:
  `D64097F09D6D29634D90F26A1F71EF49F212A5790E2DC55D9C7E2687DCBCB54E`;
  relative to the 104-session warm baseline, handles were +3, OS threads -1,
  private bytes +1,654,784 and working set +1,789,952, with zero UDP
  endpoints, stderr, plaintext hits, partial/resume files or open markers.
- r14 443-session open-order scaling checkpoint:
  `3C2A941EC6601548A4EA886FBD66378A3760C516E6B977CA267403EB87FD18A2`;
  100 polls per provider kept both medians near 0.23 ms, both maxima below
  0.54 ms and worker CPU at 8.9% of one core over 20.38 seconds.
- r14 502-session idle OS/artifact checkpoint:
  `494B56CFF8434431AA3B5A00F982CB7B09FD8FB506E6A2DF21348CCCD09CC89B`;
  versus warm-up: handles +8, OS threads -1, private bytes +1,658,880 and
  working set +1,818,624; 3,523 files passed all residual/plaintext scans.
- r14 fifteenth refreshed target/transit signed-capacity snapshots:
  `8DB5DE9E39FFCE2AF546956D999382262226D127EDBAE7D7E3921D56AF0FF6F7` /
  `2256B9CDE463A13A7459A94CBAC8E87EC451E2E0C39C0CFF688213A1924FEB3E`;
  both 15:12:10 Asia/Hong_Kong records passed Ed25519 verification.
- r14 1,289-session indexed open-order scaling checkpoint:
  `879BA79B059420ECCFFB583E658A285C0A8700DFA5F99AE669CFC60BC270ACEC`;
  100 polls per provider kept p95 below 0.68 ms and worker CPU at 4.64% of
  one core over 20.54 seconds while live sessions temporarily exposed at most
  one open order per provider.
- r14 1,295-session idle OS/artifact checkpoint:
  `7108F8EDB433B6FA2792F20C9E846E13597099839AA2B8C94D688029C3C7C881`;
  versus warm-up: handles +4, OS threads -1, private bytes +3,956,736 and
  working set -1,597,440; 9,067 files passed all residual/plaintext scans,
  with zero UDP endpoints, stderr, partial/resume files or open markers.
- r14 twenty-eighth refreshed target/transit signed-capacity snapshots:
  `2176B98186AFFCD547BE2D330DDCE8F508B56834DF01180741776D82E8355E9E` /
  `20DA499021B4B3F1B78E06B0A4576313D2FBFCEE4D1A696D5096948A0C5FD05A`;
  both 18:27:10-11 Asia/Hong_Kong records passed Ed25519 verification.
- r14 2,507-session indexed open-order scaling checkpoint:
  `9D58842A2CEBFEAA2F2B4C9DC832A0EB7C6A28E60B1D0C1DC9F7190152BA87B1`;
  100 polls per provider kept both p95 values below 0.53 ms and worker CPU at
  7.01% of one core over 20.52 seconds while live sessions exposed at most one
  open order per provider.
- r14 2,517-session idle OS/artifact checkpoint:
  `7731DA42320F82ECFECEB6ED5890B5D6A970D3DBD7E3650C88C5ED8F5322AA21`;
  versus warm-up: handles +3, OS threads -1, private bytes +6,201,344 and
  working set -1,769,472; 17,572 files passed all residual/plaintext scans,
  with zero UDP endpoints, stderr, partial/resume files or open markers.
- r14 cross-source publication invalidation snapshot:
  `4405255FDD2EEA67080D4C57AB220D9DD736BA1BA247AED814D9556C5E7A2E9E`;
  the preserved pre-stop scan covered 2,910 sessions and 20,372 files with
  zero recorded session failures or residual/plaintext findings.
- r14 stop confirmation:
  `29E8E4879D0140FE7EE15FDF697C1DABEBCB3A6C966711670362AA9981CB6171`;
  worker PID 48864 and launcher PID 52188 were absent, owned UDP endpoints
  were zero, and the final stopped progress SHA-256 was
  `8E26112F5C5370CCBF6CBEED61731E70F4F66EA2B443F233B31820FB7B2A12C3`.
- Shutdown-finalizer real integration progress/final-audit SHA-256:
  `ADF13BC5C6E2A27E5541B48EA25170959D3B68CCDFEFFF5A1A634335386BC463` /
  `A21EB05627A5F1093EB2296608735281E1EED062B654982E5E3F24EF3679ECCE`;
  four sessions completed and the independent post-exit scan passed.
- r14 live worker/launcher shutdown-negative evidence:
  `12A1D78DEFDB4CF58A1A6B22113023076948AD44BEE260E41BDF996FF47A5FBA`;
  both live-process checks exited 1 and created no final-audit output.
- Immutable-output integration r2 progress/final-audit SHA-256:
  `B2C9072EDCDF0D56D2D766E97DFF33D4BB6A8F256045B60F4512769A0E2BFF00` /
  `9F52E94990ED2A8565173587A3C19C26C1D7153499D15FD21041C644E300BC41`;
  repeat output and progress-overwrite attempts exited 1 without hash changes.
- Concurrent exclusive-publish r3 progress/final-audit SHA-256:
  `A54783800209BB1B638DCB749304B64023F92787319C11B98CE87CB7299D18F7` /
  `FD4812076B470E1683BBA354F1B7D1FFBA59BB3EDD782D727595A43387F1C4BF`;
  simultaneous finalizers exited 0/1, produced one valid report and left zero
  temporary files.
- Warm OS baseline: `73BF96E6AA16C7350A7888629E55671B7D4D6E4DCCC8E45A178CCC1113EDDDE3`.
- 233-session OS checkpoint: `3AC7BB613DACA73987A0B321A9084CF743F3A25563341D414A42464F0C422657`.
- 471-session OS checkpoint: `09806C0970DAAA93D9B574BA93C1668950F8E09DC177C74CAA81C98DF4C014BC`.

## Completion rule

Requested-feature acceptance is complete when rows A-G pass a fresh local
three-node run, both independent audits pass, and the focused peer-transit
suite is green. Functional r1 satisfies that rule.

The 86,400-second soak, a fresh one-GiB/20-session repetition, package gates
and three-public-egress physical validation are optional release qualification.
Public NAT traversal remains unclaimed until a physical run supplies nominated
srflx/prflx candidates and packet-level evidence.
