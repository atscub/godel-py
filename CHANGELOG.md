# CHANGELOG

<!-- version list -->

## v2.0.0 (2026-04-14)

### Refactoring

- **watch**: Drop --show-thinking; always suppress thought blocks
  ([`6662901`](https://github.com/atscub/godel-py/commit/6662901c33a18eb7e43d6aa5d80311c0f38c1122))

- **workflow**: Drop stream_agents kwarg; default on + --no-stream
  ([`d593fd0`](https://github.com/atscub/godel-py/commit/d593fd09f44fe8db50f44a98dad8aab4d018195a))


## v1.19.0 (2026-04-14)

### Features

- **watch**: Stream thinking + response in real time
  ([`3f32614`](https://github.com/atscub/godel-py/commit/3f326147b2bb476ea132d165767a10db19c57996))


## v1.18.0 (2026-04-14)

### Features

- **watch**: --show-thinking flag; dim thinking blocks
  ([`f8795cf`](https://github.com/atscub/godel-py/commit/f8795cfff6b5275af1328dfe1cf240d4f0fa0811))


## v1.17.1 (2026-04-14)

### Bug Fixes

- **agents**: Map Claude text blocks to agent.response, thinking to agent.thought
  ([`0852fc7`](https://github.com/atscub/godel-py/commit/0852fc79d2b46a46d3280d92dfc201c23e615054))


## v1.17.0 (2026-04-14)

### Features

- **watch**: Dedupe agent.response when it echoes the final thought
  ([`91c912a`](https://github.com/atscub/godel-py/commit/91c912a9b6a6e2ce9b9579c6964d8cb653be7167))


## v1.16.0 (2026-04-14)

### Features

- **watch**: Separate event blocks with blank lines in plain-log
  ([`5d73791`](https://github.com/atscub/godel-py/commit/5d7379168cd8fa685198e050461221dc7d935263))


## v1.15.0 (2026-04-14)

### Features

- **watch**: Suppress nested run.start events in plain-log
  ([`584bd2f`](https://github.com/atscub/godel-py/commit/584bd2f1a1c393336c7e2d7e52251e04faf13abc))


## v1.14.0 (2026-04-14)

### Features

- **watch**: Add thinking spinner while an agent prompt is in flight
  ([`3b34bb1`](https://github.com/atscub/godel-py/commit/3b34bb12d2c48e8bbb5230747b434b3d6ddd5c6c))


## v1.13.0 (2026-04-14)

### Features

- **watch**: Claude-code-style plain-log output with prompts & commands
  ([`d7aa0f1`](https://github.com/atscub/godel-py/commit/d7aa0f1783ef5377e6840abf6a4c641fc6d9bc2b))


## v1.12.0 (2026-04-14)

### Bug Fixes

- **cli**: Make --plain imply --watch in godel run
  ([`8117fb3`](https://github.com/atscub/godel-py/commit/8117fb36e6d9ccfc2cf51680bd29d0af2ea6469c))

- **run**: Address c1y review — bound proc.wait, kill on observer exception
  ([`cbbf157`](https://github.com/atscub/godel-py/commit/cbbf157e57114a797fbc08d31fca994c2e3620ea))

- **test**: Use --no-strict in test_run_plain_implies_watch
  ([`9165169`](https://github.com/atscub/godel-py/commit/9165169ce33d894bd3b73a3ed1c1fd0281e249b1))

### Features

- **cli**: Forward --plain to watcher subprocess spawned by godel run --watch
  ([`1cbd2b5`](https://github.com/atscub/godel-py/commit/1cbd2b501740b0c7b8ff4331c670b6726f14af0e))

- **run**: Stream run() stdout line-by-line; ContextVar observer for agent classification
  ([`42cfd16`](https://github.com/atscub/godel-py/commit/42cfd1625f55aee538f644a5ed0f973571c126fb))


## v1.11.0 (2026-04-14)

### Bug Fixes

- **watch**: Address 2bf review — disk-fixture AC4 test, remove dead branch
  ([`9d981ae`](https://github.com/atscub/godel-py/commit/9d981aef2f754f0dfed11096cb4afc42f6bd51da))

### Documentation

- **proposals**: Add web GUI scoping proposal for godel watch
  ([`28964ff`](https://github.com/atscub/godel-py/commit/28964ffa0f9b3407cae02486bf82a2b8ee82ef49))

- **web-gui**: Correct reducer LOC claim (130→~150 full module)
  ([`a424bed`](https://github.com/atscub/godel-py/commit/a424beda51cdb6affecaf34db62c13596396b552))

### Features

- **watch**: Truncate/summarize tool-call lines in ring buffer
  ([`6e7751f`](https://github.com/atscub/godel-py/commit/6e7751fb2bb2824a09cb3e29070da8cfc8143377))


## v1.10.1 (2026-04-14)

### Bug Fixes

- **run**: Address review C1/W1-W4/N1-N4 for SIGINT subprocess cleanup
  ([`0995a62`](https://github.com/atscub/godel-py/commit/0995a626e3b3fbb7758c06ed2869933fac341d8e))

- **run**: Isolate subprocess process groups and propagate SIGINT cleanly
  ([`b95311a`](https://github.com/atscub/godel-py/commit/b95311a6fc2ac60901560bc2891c5e01bce4ccca))


## v1.10.0 (2026-04-14)

### Features

- **watch**: Add --plain flag and GODEL_WATCH_PLAIN env var to godel watch
  ([`80db6bb`](https://github.com/atscub/godel-py/commit/80db6bbb5fe8e214d45aaacfba25c913eeb06028))

### Testing

- **watch**: Document subprocess --plain test limitations + cleanup
  ([`9af016a`](https://github.com/atscub/godel-py/commit/9af016a9c64768487468ed2abb4e54f88de7a198))


## v1.9.1 (2026-04-14)

### Bug Fixes

- **watch**: Unblock --watch + agent streaming end-to-end
  ([`20316bb`](https://github.com/atscub/godel-py/commit/20316bb08f17a46e6187b623629f19f208e3c32b))


## v1.9.0 (2026-04-14)

### Bug Fixes

- **cli**: Allow --watch subprocess spawn under strict mode
  ([`ba38d82`](https://github.com/atscub/godel-py/commit/ba38d82f4baf02d475a7d7d954e7974b3e2eff33))

### Features

- Add version() helper
  ([`936aa90`](https://github.com/atscub/godel-py/commit/936aa903d401b7867d6e0b6ae33e78108594466e))

### Testing

- Add integration test suite for observability (godel-py-5pl.16)
  ([`91cdc5d`](https://github.com/atscub/godel-py/commit/91cdc5d144f79e2d9702808570e6624276b6d66b))

- Apply pass-1 review fixes to observability integration suite
  ([`5e7a4ec`](https://github.com/atscub/godel-py/commit/5e7a4ec171620a0b0d2cbc2b996eaf81a1ffc8bd))

- Strengthen godel watch CLI subprocess assertion (pass-2 C-1)
  ([`8452f0d`](https://github.com/atscub/godel-py/commit/8452f0d228f11e0ccac85d2ce43d7d6f913c6c7d))

- **watch**: Add byte-exact syrupy snapshot test for AC1 render output
  ([`d88f836`](https://github.com/atscub/godel-py/commit/d88f836f5a75866c3603857e126eec176c26b473))

- **watch**: Pin rich<15 and tidy snapshot test call sites
  ([`daf6bd5`](https://github.com/atscub/godel-py/commit/daf6bd57ccce8d9ebbc46456738f0ad2ea37b4ac))

- **watch**: Restructure AC6 KI test to exercise real __exit__ path
  ([`125da5f`](https://github.com/atscub/godel-py/commit/125da5f4a2388687fe109aebd77013c7d3c33a76))


## v1.8.1 (2026-04-14)

### Bug Fixes

- **watch**: Hygiene nits from 5pl.11 review
  ([`9e19288`](https://github.com/atscub/godel-py/commit/9e192882da9947fa119be9184adb09374a08d7ad))


## v1.8.0 (2026-04-14)

### Bug Fixes

- **watch**: Address Pass-1 review (C-1, C-2, W-1, W-2, W-3)
  ([`a1ff529`](https://github.com/atscub/godel-py/commit/a1ff5297c3517c60f2e468e04f4b9c8ab9ad5a32))

- **watch**: Address Pass-2 review (C-1 pause, W-1 platform, W-2 psutil)
  ([`9f914bb`](https://github.com/atscub/godel-py/commit/9f914bbe56dbbb1a71adbf92aa9c1112c8a7e828))

### Features

- **cli**: Add godel run --watch and godel watch subcommand
  ([`2550545`](https://github.com/atscub/godel-py/commit/2550545f5d4efec4c8187124ded5132a116ab8bd))


## v1.7.0 (2026-04-14)

### Bug Fixes

- **watch**: Guarantee final-flush render and make signal handler async-safe
  ([`85b60ad`](https://github.com/atscub/godel-py/commit/85b60adedf3bfe32fa476cb2b208dce0a686c99c))

- **watch**: Surface lost sentinel + handle signal in EOS tail window
  ([`3700538`](https://github.com/atscub/godel-py/commit/37005385b71b67f3f31b74195f58ec0ba839b368))

### Features

- **watch**: Implement Rich TUI renderer with burst coalescing and plain fallback
  ([`66dd28c`](https://github.com/atscub/godel-py/commit/66dd28c498d708d5a259c8401244b137814d6c51))


## v1.6.0 (2026-04-14)

### Bug Fixes

- **watch**: Harden WatchModel against caller mutation + schema drift
  ([`7d083e4`](https://github.com/atscub/godel-py/commit/7d083e44f4f52628e2e1bff1f98fe6b871341bf9))

### Features

- **watch**: Add WatchModel + event-to-model reducer (godel-py-5pl.10)
  ([`041587c`](https://github.com/atscub/godel-py/commit/041587cf45e16312e1751bbf5a7387ad708c1146))


## v1.5.3 (2026-04-14)

### Bug Fixes

- **tail**: Eliminate rotation-race gap in TranscriptTail._fill_gaps
  ([`d5afb48`](https://github.com/atscub/godel-py/commit/d5afb48fbd2ceecfc7dab67a1ca8ccc3eca48e68))

### Refactoring

- **tail**: Address review — bounded retries, streaming merge, sentinel hardening
  ([`cd7179a`](https://github.com/atscub/godel-py/commit/cd7179a23f9a31a75c036aafe69597102636a4e7))


## v1.5.2 (2026-04-14)

### Bug Fixes

- **bench**: Pass-1 review fixes for observability harness
  ([`1e94c67`](https://github.com/atscub/godel-py/commit/1e94c67c6b7ab177a892d5cb056708b83032642f))

### Chores

- **bench**: Add observability benchmark harness and baseline result
  ([`f2e3b5c`](https://github.com/atscub/godel-py/commit/f2e3b5cc2385da298d10220e2249ea29ca5a8b85))

### Refactoring

- **exceptions**: Forward context kwargs through ResumeError subclasses
  ([`ce7fb23`](https://github.com/atscub/godel-py/commit/ce7fb236a3843119c4ecafb946a5d16b5bffab6c))

- **exceptions**: Make ConfigError and ResumeError GodelError subclasses
  ([`e035317`](https://github.com/atscub/godel-py/commit/e0353172855b059d81f054c030af32d4e4b90b60))


## v1.5.1 (2026-04-14)

### Bug Fixes

- **agents**: Replace sys.modules run-lookup with direct import in _invoke
  ([`427099c`](https://github.com/atscub/godel-py/commit/427099ccb0d83412a48a91695ee83d0ac9361cae))

### Refactoring

- **tests**: Extract stamped_stream_path async context manager
  ([`8ff6dc0`](https://github.com/atscub/godel-py/commit/8ff6dc0929fa3df95fbf25b618395f7a1e4af25b))


## v1.5.0 (2026-04-14)

### Bug Fixes

- **capture**: Pass-1 review fixes for stdout capture (godel-py-5pl.7)
  ([`ea01aa6`](https://github.com/atscub/godel-py/commit/ea01aa66e74d530b77d140f2303e171b670be637))

### Features

- **capture**: Implement stdout capture pipe-per-step with transcript wiring (godel-py-5pl.7)
  ([`a413bb6`](https://github.com/atscub/godel-py/commit/a413bb656147b9d08d4337c60bf40dba966d3e60))


## v1.4.0 (2026-04-14)

### Features

- **redact**: Redaction infrastructure with string-based pipeline (godel-py-5pl.6)
  ([`58298b8`](https://github.com/atscub/godel-py/commit/58298b8f20d72ad7eee56a169932579843a725c6))

### Refactoring

- **redact**: Address pass-1 review (godel-py-5pl.6)
  ([`7fb39fa`](https://github.com/atscub/godel-py/commit/7fb39fa8ef8019ecf392849c383ebfa1a088054c))


## v1.3.0 (2026-04-14)

### Bug Fixes

- **agents**: Emit all content blocks from multi-block assistant events; add UTF-8 tests
  ([`5fb6df4`](https://github.com/atscub/godel-py/commit/5fb6df4ace230ab6c89d76429896cf2d7c35bafa))

### Features

- **agents**: Wire Copilot + Claude agents to transcript (godel-py-5pl.4)
  ([`77b6083`](https://github.com/atscub/godel-py/commit/77b608320573e22c36c188040249c8b3ffbfdb3b))

- **tail**: Replace monolithic _fmt_event allow-list with formatter registry (godel-py-5pl.9)
  ([`3581758`](https://github.com/atscub/godel-py/commit/358175811e065fe274068ea6039400a200e1e009))


## v1.2.2 (2026-04-13)

### Bug Fixes

- **agents**: Emit FAILED event on CancelledError in agent.call
  ([`389194a`](https://github.com/atscub/godel-py/commit/389194a0ba582d2861fb1d3263238838695e8c2a))

- **agents**: Narrow cancel catch scope and guard log-write failures
  ([`54770a1`](https://github.com/atscub/godel-py/commit/54770a15da17cff378e05ab1cf3bacaea9045ed8))

### Documentation

- Align redactor signature with source, qualify GODEL_NO_CAPTURE, add planned ops (godel-py-5pl.14)
  ([`fec0618`](https://github.com/atscub/godel-py/commit/fec061844fd9ceb0d2fbde3beb2eea1e9c1ecb19))

- Rescope observability pages to master-today behavior (godel-py-5pl.14)
  ([`34c7a97`](https://github.com/atscub/godel-py/commit/34c7a978b798a63c468b2648d4b40d5493985677))

- Transcript format, redaction, and stdout-capture guides (godel-py-5pl.14)
  ([`a9df48c`](https://github.com/atscub/godel-py/commit/a9df48c96b73ee87aacd8cab6b772457c08f6257))


## v1.2.1 (2026-04-13)

### Bug Fixes

- **transcript**: Drop seq field from rotation sentinels to prevent seq collision
  ([`45c4d03`](https://github.com/atscub/godel-py/commit/45c4d03538b34e28664fea67e3d263323fc13b9c))

- **transcript**: Suppress header-only rotation; document reader contract
  ([`e221d09`](https://github.com/atscub/godel-py/commit/e221d0995bfe917070150c382bd279f968bace48))


## v1.2.0 (2026-04-13)

### Bug Fixes

- **agents**: Drain oversized lines correctly in stream parser (godel-py-5pl.3)
  ([`b559aef`](https://github.com/atscub/godel-py/commit/b559aeffa7e46eaed92a6c32299a91282a6663e5))

### Features

- **agents**: Add tolerant streaming JSONL parser (godel-py-5pl.3)
  ([`a278eb1`](https://github.com/atscub/godel-py/commit/a278eb154417321d7bf6aaff344cd3c7d9b59698))


## v1.1.0 (2026-04-13)

### Bug Fixes

- Resolve merge conflict markers in __init__ and _exceptions
  ([`def1444`](https://github.com/atscub/godel-py/commit/def1444212502a87e7db240e32f7191e59d0802b))

- **decorators**: Address adversarial review pass 1 on 5pl.8
  ([`a45dce5`](https://github.com/atscub/godel-py/commit/a45dce537130b12a06f9c788ce2f5b20ddd332c2))

- **observability**: Guard stream_path contextvar against early-exit leak
  ([`fbb0fbd`](https://github.com/atscub/godel-py/commit/fbb0fbd79da0ab95ffc4f60ab76fdc17dfab36da))

- **transcript**: Address adversarial review P1 findings
  ([`f0cc2dc`](https://github.com/atscub/godel-py/commit/f0cc2dc70fd8a8a66d566fbd8e4bd8809d25bbcf))

### Chores

- Add local obsidian vault and plans directory
  ([`f6143cf`](https://github.com/atscub/godel-py/commit/f6143cf8d6fead275422dafcb183cbc7079a5276))

### Documentation

- Migrate legacy DSL-era docs into library voice
  ([`55f3c09`](https://github.com/atscub/godel-py/commit/55f3c09d60ab752b2f397c27b47615ffed140d7f))

### Features

- Add rich as optional dep under godel[watch] with import shim
  ([`c28701c`](https://github.com/atscub/godel-py/commit/c28701c55df00e851ebfa8c55597b5264605506b))

- **decorators**: Add stream_agents, capture_stdout, redact options to @workflow and @step
  ([`08f3d9f`](https://github.com/atscub/godel-py/commit/08f3d9f6ce9f6f6f522769a30505d05745b07f9d))

- **observability**: Stamp stream_path on every transcript event at launch time
  ([`8bb58a1`](https://github.com/atscub/godel-py/commit/8bb58a1349e48314de247519cbf1e7bdd0c36de9))

- **transcript**: Add TranscriptWriter with JSONL format v1 and size-capped rotation
  ([`6f3b836`](https://github.com/atscub/godel-py/commit/6f3b8366e7ba1d0c4784c7cde9a06c46364e683b))

### Refactoring

- **watch**: Apply review fixes for 5pl.13
  ([`2e17b90`](https://github.com/atscub/godel-py/commit/2e17b9090d00048982b94ae1ae7799089478f104))


## v1.0.1 (2026-04-13)

### Bug Fixes

- Sync version in pyproject.toml and track it in semantic-release
  ([`d1fd5d1`](https://github.com/atscub/godel-py/commit/d1fd5d15bddc2d3090526cba92190c2a587b1880))


## v1.0.0 (2026-04-13)

- Initial Release
