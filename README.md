# makepatch

Hatchling과 연동하는 Git 기반 Python 패키지 패치 도구입니다. [paperweight](https://github.com/PaperMC/paperweight)와 `pnpm patch`에서 영감을 받았습니다.

| 모드 | 방식 | 결과물 | 사용 가능한 패치 종류 |
| --- | --- | --- | --- |
| 소스 패치 (`makepatch src`) | paperweight 방식: upstream Git 저장소를 고정 ref로 가져와 패치 | 새 패키지(포크)의 sdist/wheel | 소스 패치, 기능 패치 |
| 패키지 패치 (`makepatch pkg`) | pnpm 방식: 설치된 패키지를 site-packages에서 패치 | 패치된 가상 환경 | 소스 패치 |

패치 종류는 다음 두 가지입니다.

- **소스 패치**: `git diff`로 만들고 `git apply`로 적용합니다. 파일 하나당 패치 하나입니다.
- **기능 패치**: `git format-patch -p --minimal --zero-commit`으로 만들고 `git am --3way`로 적용합니다. 커밋 하나당 패치 하나이며, 소스 패치 모드에서만 사용할 수 있습니다.

모든 패치는 정적으로 적용되며, 런타임 monkey-patching은 하지 않습니다. patchutils 없이 `git`만 사용합니다.

## 요구 사항

- Python ≥ 3.10
- git ≥ 2.32 (`GIT_CONFIG_GLOBAL` 사용)
- uv 또는 pip

## 소스 패치 모드

포크 저장소에는 `pyproject.toml`과 `patches/`만 있으면 됩니다.

```toml
[build-system]
requires = ["hatchling", "makepatch"]
build-backend = "hatchling.build"

[project]            # 포크 패키지의 메타데이터는 직접 정의합니다.
name = "requests-fork"
version = "2.32.3.post1"
dependencies = ["urllib3>=1.21.1,<3", "idna>=2.5,<4", "charset_normalizer>=2,<4", "certifi>=2017.4.17"]

[tool.makepatch.source]
upstream = "https://github.com/psf/requests.git"
ref = "v2.32.3"                           # 커밋 SHA(40자리) 권장, 태그·브랜치 가능
include = { "src/requests" = "requests" } # upstream 경로 → wheel 경로
exclude = ["**/*.pyi"]                    # sdist/wheel에서 제외 (include 경로 기준)
work-exclude = ["docs/", "tests/"]        # work/·빌드 트리에서 제외 (upstream 루트 기준)
# work-dir = "work"                       # 기본값
# patches-dir = "patches"                 # 기본값

[tool.hatch.build.hooks.makepatch]        # 빌드 훅 활성화 (설정은 위 테이블에 둡니다)

[tool.hatch.build.targets.wheel]
bypass-selection = true
```

`.gitignore`에는 `work/`와 `.makepatch/`를 추가하십시오. sdist에서 제외하기 위해서입니다.

### 파일 제외

두 옵션 모두 gitignore 문법(`!` 부정 포함)을 사용하지만, 기준 경로와 효과가 다릅니다.

| 옵션 | 기준 경로 | 효과 |
| --- | --- | --- |
| `exclude` | 각 `include` 경로 (예: `src/requests`) | sdist와 wheel에서 제외합니다. `work/`에는 그대로 있습니다. |
| `work-exclude` | upstream 루트 | `git sparse-checkout`으로 `work/`와 빌드 트리에 체크아웃하지 않습니다. 시간과 용량을 줄이는 용도입니다. |

- `work-exclude`로 제외한 파일에 패치가 있으면 `makepatch src setup`과 빌드가 오류로 중단됩니다.
- `work-exclude`를 바꾼 뒤에는 `makepatch src setup`을 다시 실행하십시오.

### 작업 흐름

```sh
makepatch src setup     # upstream fetch → work/ 생성 → 소스 패치 적용 → 기능 패치 git am
# work/에서 파일 수정
makepatch src fixup     # 작업 트리 변경을 소스 패치 커밋에 흡수
# 또는 work/에서 일반 커밋 → 기능 패치
makepatch src rebuild   # patches/sources/**, patches/features/*.patch 재생성
makepatch src status
```

`work/` 저장소의 구조는 다음과 같습니다.

```
<upstream ref>                 tag makepatch/base
makepatch: source patches      tag makepatch/sources   ← patches/sources/<경로>.patch
<기능 커밋> ...                                          ← patches/features/NNNN-*.patch
```

기능 패치 적용이 충돌하면 `git am` 세션이 남습니다. `work/`에서 충돌을 해결하고 `git am --continue`를 실행한 뒤 `makepatch src rebuild`를 실행하십시오.

### 빌드

```sh
uv build
```

- 빌드 훅은 `work/`를 사용하지 않고 `.makepatch/build/tree`에 패치를 새로 적용합니다. 따라서 커밋하지 않은 작업은 결과물에 섞이지 않습니다.
- sdist에는 패치가 적용된 소스(`_makepatch/tree/`)가 들어갑니다. sdist에서 wheel을 빌드할 때는 git도 네트워크도 필요하지 않습니다.
- `MAKEPATCH_OFFLINE=1`을 설정하면 `.makepatch/upstream.git` 캐시만 사용합니다.
- upstream의 자체 빌드 단계(C 확장 등)는 실행하지 않습니다. 순수 Python 소스가 대상입니다.

## 패키지 패치 모드

makepatch를 프로젝트의 개발 의존성으로 설치합니다.

```sh
uv add --dev makepatch
```

```sh
uv run makepatch pkg edit requests     # .makepatch/edit/requests@2.32.3/ 에 편집용 사본 생성
# 사본의 파일 수정
uv run makepatch pkg commit requests   # patches/packages/requests@2.32.3.patch 저장 후 적용
uv run makepatch pkg apply             # 모든 패치 적용 (--check: 적용 가능 여부만 확인)
uv run makepatch pkg status
uv run makepatch pkg revert requests   # 원본 파일로 복원
```

- 패치 파일 이름은 `<정규화된 이름>@<버전>.patch`이고, 경로는 site-packages 기준입니다. 설치된 버전이 다르면 오류가 발생합니다.
- 패치 파일을 삭제하고 `pkg apply`를 실행하면 해당 패키지가 원본으로 복원됩니다.
- 패치 디렉터리는 `[tool.makepatch.packages] patches-dir`로 바꿀 수 있습니다.
- `--python <인터프리터>`로 다른 환경을 대상으로 지정할 수 있습니다.
- editable 설치는 대상이 아닙니다. 소스를 직접 수정하십시오.

### uv와의 호환성

- **캐시 보호**: uv는 Linux에서 기본적으로 캐시의 파일을 hardlink로 설치합니다(`--link-mode`로 clone·copy·symlink 선택 가능). makepatch는 파일을 제자리에서 수정하지 않습니다. 변경할 파일을 임시 디렉터리에서 `git apply`한 뒤 `os.replace`로 교체하므로, 교체된 경로만 새 inode를 갖고 uv 캐시는 변경되지 않습니다. symlink 모드에서도 같습니다.
- **기록**: `RECORD`의 해시를 갱신하고, 적용 기록(`makepatch.json`, `makepatch.patch`)을 dist-info에 남겨 `RECORD`에 등록합니다. 패키지를 제거하면 함께 삭제됩니다.
- **재설치 복구**: `uv sync`는 설치된 파일의 내용을 검사하지 않으므로 패치가 유지됩니다. 재설치나 버전 변경으로 패치가 사라지면, makepatch가 설치하는 `makepatch-startup.pth`가 인터프리터 시작 시 이를 감지해 다시 적용합니다.
  - 평소에는 패치 파일과 마커의 해시만 비교합니다.
  - 불일치가 있을 때만 환경 잠금을 잡고 `pkg apply`와 같은 작업을 수행합니다.
  - `MAKEPATCH_DISABLE_STARTUP=1`로 끌 수 있습니다.
  - uv가 인터프리터를 조회할 때처럼 `-I`(isolated)로 실행되면 건너뜁니다.

## Vercel 빌드에서 사용

Vercel 빌드 이미지는 Amazon Linux 2023 기반입니다. `git`은 사전 설치 패키지 목록에 있지만, `uv`는 목록에 없으므로 별도 설치가 필요할 수 있습니다. 빌드 단계에서는 시작 훅에 의존하지 말고, 설치 직후 `pkg apply`를 명시적으로 실행하는 것을 권장합니다. 적용에 실패하면 종료 코드 1로 빌드가 중단됩니다.

```json
{
  "installCommand": "uv sync --frozen && uv run makepatch pkg apply"
}
```

## 개발

```sh
uv sync
uv run pytest
```
