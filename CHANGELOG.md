# CHANGELOG

<!-- version list -->

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
