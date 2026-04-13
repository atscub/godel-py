# CHANGELOG

<!-- version list -->

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
