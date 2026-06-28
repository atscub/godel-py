# CHANGELOG

<!-- version list -->

## v3.17.1 (2026-06-28)

### Bug Fixes

- Address review findings — docstring, max_tokens pattern, model attribution
  ([#17](https://github.com/atscub/godel-py/pull/17),
  [`f802ac3`](https://github.com/atscub/godel-py/commit/f802ac3e6d9a3af18bba0471b0b971748595abaf))

- Auto-recover from agent context overflow ([#17](https://github.com/atscub/godel-py/pull/17),
  [`f802ac3`](https://github.com/atscub/godel-py/commit/f802ac3e6d9a3af18bba0471b0b971748595abaf))

- Auto-recover from agent context overflow with fresh session retry
  ([#17](https://github.com/atscub/godel-py/pull/17),
  [`f802ac3`](https://github.com/atscub/godel-py/commit/f802ac3e6d9a3af18bba0471b0b971748595abaf))

- Leave compact() unimplemented until proper compaction is designed
  ([#17](https://github.com/atscub/godel-py/pull/17),
  [`f802ac3`](https://github.com/atscub/godel-py/commit/f802ac3e6d9a3af18bba0471b0b971748595abaf))

- Raise ContextOverflowError on agent context overflow, add compact()
  ([#17](https://github.com/atscub/godel-py/pull/17),
  [`f802ac3`](https://github.com/atscub/godel-py/commit/f802ac3e6d9a3af18bba0471b0b971748595abaf))

### Chores

- Add lint+test-before-push rule, export ContextOverflowError
  ([#17](https://github.com/atscub/godel-py/pull/17),
  [`f802ac3`](https://github.com/atscub/godel-py/commit/f802ac3e6d9a3af18bba0471b0b971748595abaf))

### Refactoring

- Move e2e tests from examples/ to e2e_tests/ ([#17](https://github.com/atscub/godel-py/pull/17),
  [`f802ac3`](https://github.com/atscub/godel-py/commit/f802ac3e6d9a3af18bba0471b0b971748595abaf))


## v3.17.0 (2026-06-28)

### Bug Fixes

- Address review findings on rewind --assume-idempotent
  ([#16](https://github.com/atscub/godel-py/pull/16),
  [`e126188`](https://github.com/atscub/godel-py/commit/e1261881379c1e240b34f8ec22a506f1f5272b3c))

### Documentation

- Add GitHub issue triage guide
  ([`a5fd0c8`](https://github.com/atscub/godel-py/commit/a5fd0c8c57e0597fa02acf9abc326846ad5eeba8))

- Add rewind --assume-idempotent demo workflow and test script
  ([#16](https://github.com/atscub/godel-py/pull/16),
  [`e126188`](https://github.com/atscub/godel-py/commit/e1261881379c1e240b34f8ec22a506f1f5272b3c))

### Features

- Add --assume-idempotent flag to rewind command ([#16](https://github.com/atscub/godel-py/pull/16),
  [`e126188`](https://github.com/atscub/godel-py/commit/e1261881379c1e240b34f8ec22a506f1f5272b3c))


## v1.0.0 (2026-06-28)

- Initial Release

## v3.16.7 (2026-06-28)

### Bug Fixes

- Drop Python 3.10/3.11 support, fix test issues from review
  ([#14](https://github.com/atscub/godel-py/pull/14),
  [`6b58a25`](https://github.com/atscub/godel-py/commit/6b58a25f29a4fe013ff2d0aaf97bfa1e85144882))

- Import guard broken on Python 3.12 ([#14](https://github.com/atscub/godel-py/pull/14),
  [`6b58a25`](https://github.com/atscub/godel-py/commit/6b58a25f29a4fe013ff2d0aaf97bfa1e85144882))

- Import guard broken on Python 3.12, simplify CI matrix
  ([#14](https://github.com/atscub/godel-py/pull/14),
  [`6b58a25`](https://github.com/atscub/godel-py/commit/6b58a25f29a4fe013ff2d0aaf97bfa1e85144882))

### Continuous Integration

- Update publish workflow to Python 3.12
  ([`74f12ba`](https://github.com/atscub/godel-py/commit/74f12bab0c07f5bcef2b903cda9a04c17b89061f))


## v3.16.6 (2026-06-28)

### Bug Fixes

- Address PR review feedback
  ([`4e141e5`](https://github.com/atscub/godel-py/commit/4e141e5ddc25876171c1d8420035978daad1f857))

- **tail**: Fix race in late-attach archive replay causing event loss
  ([`b232943`](https://github.com/atscub/godel-py/commit/b23294341c38d031de631be29110cacd7644bc0f))

### Continuous Integration

- Switch to on-demand code review via @claude comments
  ([#13](https://github.com/atscub/godel-py/pull/13),
  [`5206431`](https://github.com/atscub/godel-py/commit/5206431ad337d20f308da81d0c7976ef02bdf719))

### Documentation

- Improve code review output format with inline comments and suggestions
  ([`afb0980`](https://github.com/atscub/godel-py/commit/afb09800b187b7662ede5e8330ac51c96c6ab049))


## v3.16.5 (2026-06-28)

### Bug Fixes

- **ci**: Use direct prompt for code review instead of plugin
  ([#12](https://github.com/atscub/godel-py/pull/12),
  [`3189f7d`](https://github.com/atscub/godel-py/commit/3189f7d37dbf87ecafab451a31de8f9d6667ec86))


## v3.16.4 (2026-06-26)

### Bug Fixes

- **ci**: Grant write permission so code review can post PR comments
  ([#10](https://github.com/atscub/godel-py/pull/10),
  [`c8356e8`](https://github.com/atscub/godel-py/commit/c8356e879fed57ce30a152c43c08f55caacd46b7))


## v3.16.3 (2026-06-26)

### Bug Fixes

- Address second review — crash paths, replay hash, stale docs
  ([`dd95f10`](https://github.com/atscub/godel-py/commit/dd95f10399c87c44098efc93ac38d61337cf6f62))

- Restore design-intent comments removed during refactor
  ([`cc1196e`](https://github.com/atscub/godel-py/commit/cc1196ecf13494cdb22e40c98513eab17e6c90d8))

- Shell-quoting bug, read_text truncation, parallel() docs (#2, #3, #4)
  ([`df0d30f`](https://github.com/atscub/godel-py/commit/df0d30fc1fb09e05e89bbec20e77c5ce7d4baa04))

- Third review — replay hash compat, copilot docstrings, style
  ([`fc2bfea`](https://github.com/atscub/godel-py/commit/fc2bfea94d40ddd6c0124eceacbc51b6bd152a95))

- **io**: Address review findings for read_text cache
  ([`d3a6429`](https://github.com/atscub/godel-py/commit/d3a6429702062392b06e1d952d9cc249e2e4a4be))

### Refactoring

- **io**: Rename cache→replay, split _cache_dir, add edge-case tests
  ([`87c5416`](https://github.com/atscub/godel-py/commit/87c5416c8bf1567b27fcdddbceff8cbf95c9b86c))

### Testing

- Add regression tests for issues #2, #3, #4
  ([`dc4b60a`](https://github.com/atscub/godel-py/commit/dc4b60a8bd3808996795e78c3dbfd1949d402cc4))

- Distribute regression tests into module test files
  ([`49591ee`](https://github.com/atscub/godel-py/commit/49591ee82d84d376d718bdc5a8badd5f2ac8e20c))


## v3.16.2 (2026-06-17)

### Bug Fixes

- **examples**: Remove human checkpoint from content_pipeline
  ([`d0d334f`](https://github.com/atscub/godel-py/commit/d0d334fdd012fc17df6350f174318187310cc53c))


## v3.16.1 (2026-06-17)

### Bug Fixes

- **examples**: Tested end-to-end, add examples README
  ([`f650d04`](https://github.com/atscub/godel-py/commit/f650d042dc068b12ee518c3edfcf4b3975d19378))

### Chores

- **examples**: Remove trivial demo workflows
  ([`eddc4f1`](https://github.com/atscub/godel-py/commit/eddc4f147b96bee34f5a4c7367f4f2802b0a57b1))


## v3.16.0 (2026-06-17)

### Features

- **examples**: Add incident response, content pipeline, and data quality workflows
  ([`c78a256`](https://github.com/atscub/godel-py/commit/c78a256e3ca959dc5800236a191fea4d703727c6))


## v3.15.1 (2026-06-17)

### Bug Fixes

- **examples**: Capture ruff output on non-zero exit
  ([`9c717e3`](https://github.com/atscub/godel-py/commit/9c717e3b43d6559de45e832b09c66b6f3236158a))


## v3.15.0 (2026-06-17)

### Features

- **examples**: Richer audit report with structured data sections
  ([`873e004`](https://github.com/atscub/godel-py/commit/873e0042740f73fbc8b7bf0a0e0e8f7732f935e0))


## v3.14.1 (2026-06-17)

### Bug Fixes

- **examples**: Fix codebase_audit run() signature and det.now() usage
  ([`a6430ad`](https://github.com/atscub/godel-py/commit/a6430adc3697a864c858f90ada78c7f9212c47a9))


## v3.14.0 (2026-06-17)

### Chores

- Remove outdated godel-lang references
  ([`633efa5`](https://github.com/atscub/godel-py/commit/633efa5e661e8a4f9d262c530b3848135e0f6e8f))

- **license**: Reduce BSL change date from 6 to 3 years
  ([`6442192`](https://github.com/atscub/godel-py/commit/6442192db6f0c709db8f9b2a956662234d432ff6))

### Documentation

- Rewrite README for public launch
  ([`19998b7`](https://github.com/atscub/godel-py/commit/19998b72b25f754082886fcceb6d3bd85d2c9166))

### Features

- **examples**: Add codebase audit workflow
  ([`450c7ff`](https://github.com/atscub/godel-py/commit/450c7ff6e9b30cabd56ac1bfb36c99f2ba5da265))


## v3.13.2 (2026-06-17)

### Bug Fixes

- **ci**: Add contents:read permission to pypi job
  ([`d654c80`](https://github.com/atscub/godel-py/commit/d654c80d06d8e95d1315154f4ed87e64697078b3))


## v3.13.1 (2026-06-17)

### Bug Fixes

- **tests**: Fix flaky late-attach test on slow CI runners
  ([`a111748`](https://github.com/atscub/godel-py/commit/a1117480f4791596b8a832697426ba1a6648bc4e))

### Continuous Integration

- Retrigger release after tag realignment
  ([`d60e3c6`](https://github.com/atscub/godel-py/commit/d60e3c6966064b350b743d3d2a820ff774146705))


## v3.13.0 (2026-04-17)

### Bug Fixes

- **agents**: Shlex-quote session_id in --resume flag + exclude from replay hash
  ([`545a68b`](https://github.com/atscub/godel-py/commit/545a68bc30053bdf4e4e261e7feff1e2e7043cf2))

### Features

- **agents**: Add session_id ctor param and accessor to resume CLI sessions across processes
  ([`e23579d`](https://github.com/atscub/godel-py/commit/e23579d6576aa1738d24198acb71b579c246fdbf))


## v3.12.0 (2026-04-17)

### Bug Fixes

- **watch**: Apply verbosity filtering in TUI mode + parallel line-cap test
  ([`cbc5ca1`](https://github.com/atscub/godel-py/commit/cbc5ca1b39e30a022567c4c2288cf4316ad91a5a))

### Features

- **watch**: High-level-by-default output with verbosity controls
  ([`5699216`](https://github.com/atscub/godel-py/commit/56992160b227540d32b98420ef00be8176a7c084))


## v3.11.0 (2026-04-17)

### Bug Fixes

- **show**: Propagate TranscriptTailError instead of silently swallowing
  ([`313607c`](https://github.com/atscub/godel-py/commit/313607cf09b5a9b3822ce941cfb7d28186091867))

- **watch**: Suppress [bN] branch prefix for sequential single-agent streams
  ([`00aaaa8`](https://github.com/atscub/godel-py/commit/00aaaa84df978a2c02436d94f92c497e29afae0b))

- **watch**: Use step_path→root mapping for active-root cleanup on step.exit
  ([`9c97311`](https://github.com/atscub/godel-py/commit/9c973117cf9ec6b0620d26414f16ce644a62abcb))

### Documentation

- **web-gui**: Fix Section 4 LOC arithmetic and Phase A framing
  ([`98f12aa`](https://github.com/atscub/godel-py/commit/98f12aa2444bad12b89356593caddf6c4ad88543))

### Features

- **show**: Add --full flag to retrieve untruncated agent request/response
  ([`1e958df`](https://github.com/atscub/godel-py/commit/1e958df98be02b8121740c9973521ff1345ad0ef))


## v3.10.1 (2026-04-16)

### Bug Fixes

- **strict-ast**: Detect aliased from-import sleep bypassing the ban
  ([`0ca01b4`](https://github.com/atscub/godel-py/commit/0ca01b49416405a48f0a2cdf4c045766e6b3b765))


## v3.10.0 (2026-04-16)

### Features

- **formatters**: Surface path and byte-count in read_text/write_text display
  ([`e177103`](https://github.com/atscub/godel-py/commit/e1771033a9c0b6b280f4a9abb18187c7180405da))


## v3.9.0 (2026-04-16)

### Bug Fixes

- **retry**: Pass-1 CRITICAL + WARN fixes for det.sleep replay + backoff validation
  ([`268af5f`](https://github.com/atscub/godel-py/commit/268af5f504947e05f257e478b34beec111adad5c))

### Features

- **retry**: Add exponential backoff via backoff_seconds and backoff_multiplier
  ([`68e3b8f`](https://github.com/atscub/godel-py/commit/68e3b8f7d4bd8ff0f377160315f77a12ce367831))


## v3.8.0 (2026-04-16)

### Bug Fixes

- **timeout**: Pass-1 CRITICAL + WARN fixes for @step(timeout=N) cancellation propagation
  ([`b41e253`](https://github.com/atscub/godel-py/commit/b41e253d7745d95362c24a9c3e476c00baaf842e))

### Features

- **step**: Add timeout=N parameter for per-step wall-clock cancellation
  ([`0c3bf9d`](https://github.com/atscub/godel-py/commit/0c3bf9d77d3e24754c391c2c1917a6337f220f8f))


## v3.7.0 (2026-04-16)

### Bug Fixes

- **idempotency**: Apply C1/C2/C3 critical fixes for opt-in idempotency
  ([`2c5fae8`](https://github.com/atscub/godel-py/commit/2c5fae825d1ac59c5ac76667325d8e5e5f84b603))

- **idempotency**: Pass-2 C4/C5/C6 — request_hash exclusions + system_prompt restore
  ([`c85433c`](https://github.com/atscub/godel-py/commit/c85433c587b87320f2503d501afe53bd6541d2e6))

### Features

- **idempotency**: Opt-in idempotency at step, call, and run levels
  ([`2593050`](https://github.com/atscub/godel-py/commit/2593050fc7d6c8bd845675e155f99b110ce52363))


## v3.6.0 (2026-04-16)

### Bug Fixes

- **agents**: Apply all pass-1 review findings (C1, C2, W1–W4)
  ([`f87dea9`](https://github.com/atscub/godel-py/commit/f87dea965fef9393fa52bc6cf1a5e7f04005e8b3))

### Features

- **agents**: Add system_prompt kwarg — set once, not repeated per call
  ([`0eb7c08`](https://github.com/atscub/godel-py/commit/0eb7c088597152960f6db20b7942babfd20633c2))


## v3.5.0 (2026-04-16)


## v3.4.0 (2026-04-16)

### Bug Fixes

- **io**: Address critical review issues on read_text / write_text
  ([`b663cc8`](https://github.com/atscub/godel-py/commit/b663cc851f01ad97468c7e90de3d0ed8ba520906))

- **io**: Address pass-2 WARN review findings in io.py
  ([`a33cd28`](https://github.com/atscub/godel-py/commit/a33cd28e821d395c5c7dce6b67a958fc163045f1))

### Features

- **io**: Add audited read_text / write_text primitives
  ([`6fc600f`](https://github.com/atscub/godel-py/commit/6fc600fd947b34d80d70e41897629b6a041d951c))


## v3.3.0 (2026-04-16)

### Bug Fixes

- **io**: Defer non-TTY warning to live-read path + docs/help polish
  ([`dfef7e0`](https://github.com/atscub/godel-py/commit/dfef7e0a00deb25bca87f10ef9725f27765b57e7))

- **io**: Exclude auto_checkpoint from replay request_hash
  ([`ec91067`](https://github.com/atscub/godel-py/commit/ec91067f226dabdafd7336de6e120943637a0fdc))

### Features

- **io**: Support programmatic checkpoint answers via stdin
  ([`f5328b8`](https://github.com/atscub/godel-py/commit/f5328b8d68d921bddd0afb3abc54a124d93bdded))


## v3.2.0 (2026-04-16)

### Documentation

- **examples**: Add feature_factory workflow + monitoring guide
  ([`3d7b858`](https://github.com/atscub/godel-py/commit/3d7b858d4983d620e9c79ebe2b30c9f1daad4300))

- **guide**: Add best-practices guide
  ([`d9ed5ac`](https://github.com/atscub/godel-py/commit/d9ed5ac01e1c15bde9a098b404a43201e253db56))

- **guide**: Expose monitoring guide via `godel guide`
  ([`25e2432`](https://github.com/atscub/godel-py/commit/25e2432ab8aa663f06d59f129be327252cde8c42))

- **monitoring**: Clarify rewind marks events invalidated (log is append-only)
  ([`bba22e9`](https://github.com/atscub/godel-py/commit/bba22e9ecec5779ac66ed98c83de861ea2dac066))

- **monitoring**: Improved documents
  ([`6a0851d`](https://github.com/atscub/godel-py/commit/6a0851dd16e7fa3dda1c5f01da750a511f0032eb))

- **monitoring**: Note godel tail streams all events (no invalid/retry filtering)
  ([`3237736`](https://github.com/atscub/godel-py/commit/3237736403624b187bda99f981a3929f7812cd45))

- **monitoring**: Prefer godel tail over raw file seek; add rewind-then-resume recovery
  ([`2ea1aa2`](https://github.com/atscub/godel-py/commit/2ea1aa2fbf3d87e33aad79bdd8c6e13904176c48))

### Features

- **cli**: Add godel runs list command
  ([`74c3683`](https://github.com/atscub/godel-py/commit/74c3683b36cf4e90868a7ace672ff6ba8dccc186))


## v3.1.0 (2026-04-14)

### Bug Fixes

- **decorators**: Inherit stream_agents + transcript in parallel() branches
  ([`4070e5d`](https://github.com/atscub/godel-py/commit/4070e5db873e6dc583adac1470327dcc8015cba7))

### Documentation

- Audit and refresh against 3.0.0 CLI + config surface
  ([`962dae1`](https://github.com/atscub/godel-py/commit/962dae189fee8ffd34a578048db9119a58c48af9))

### Features

- **watch**: Tag parallel branches with [bN] in plain line-log
  ([`6f532e0`](https://github.com/atscub/godel-py/commit/6f532e001c5fa43291abc09197f87a3fb4340b32))


## v3.0.1 (2026-04-14)

### Bug Fixes

- **agents**: Map Copilot CLI 1.0.25+ tool.execution_{start,complete} events
  ([`3329bb7`](https://github.com/atscub/godel-py/commit/3329bb7e49ac7b8bb10386f34365f6f1f53db005))

- **watch**: Order status lines after watcher and wrap long streamed lines
  ([`ded35e3`](https://github.com/atscub/godel-py/commit/ded35e3e6bf05eea3d6810002b1cbe5f91b0ceff))


## v3.0.0 (2026-04-14)

### Features

- **cli**: Add `godel guide` for bundled agent onboarding docs
  ([`3aa0184`](https://github.com/atscub/godel-py/commit/3aa018422873b171ea5f52cfac88987464440413))

- **config**: Two-tier .godel/ + ~/.godel/ config with named workflows
  ([`2a0d8dc`](https://github.com/atscub/godel-py/commit/2a0d8dc267660274b79756cc632abe3f70fdbb52))


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
