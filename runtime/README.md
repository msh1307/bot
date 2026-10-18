# EVO inference runtime patches

EVO-X2 64GB / gfx1151에서 작업한 추론 엔진 변경과 과거 비교 결과를 보관한다.
Discord/OMP 코드는 `../omp-migration/`, 호스트 승인 코드는 `../host-tools/`에 있다.

## 구성과 상태

| 파일 | 내용 | 상태 |
|---|---|---|
| `strata/evo-v0.1.43.patch` | startup direct I/O, staging 반환, CPU vision의 필요 시 실행·유휴 종료, 실시간 응답 지표 | 기존 배포 소스에서 추출 |
| `vision/llama-mmap.patch` | CPU projector 가중치의 읽기 전용 mmap | 기존 배포용 helper 변경 |
| `vision/strata-bitmap-lifetime.patch` | 전처리 뒤 원본 bitmap 조기 반환 | 기존 배포용 helper 변경 |
| `qwen/experiments/adaptive-dflash.patch` | DFlash2 acceptance 기반 draft 길이 조절 | 실측 회귀로 미채택 |

2026-10-18에 Strata 패치를 서버 후보 소스의 실제 `git diff HEAD`와 대조해
바이트 일치를 확인했다. 이번 정리에서는 새 빌드·추론·성능 시험을 실행하지 않았다.
과거 결과와 한계는 [RESULTS.md](RESULTS.md), 기준 revision과 패치 SHA-256은
[manifest.json](manifest.json)에 있다. 소스 일치와 모델 품질 동등성은 별개다.

## 적용 기준

- Strata 전체 패치: `Niko1221/Strata`, v0.1.43,
  `7695859aa13afb248bdb4d36f9b2e95707db079d`.
- `vision/llama-mmap.patch`: llama.cpp
  `3cf03257f219afbe7334045ff7c6a06ac68c627d`의 `tools/mtmd/clip.cpp`.
- bitmap 패치는 **Strata**의 `tools/vision/strata_vision.cpp`에 적용한다.
  이 별도 helper의 정확한 과거 기준 commit은 회수하지 못했다. 대상 소스에서
  적용 가능 여부와 전처리 객체의 수명을 다시 확인해야 한다.
- adaptive 패치의 과거 기준은 `a043d38a62aeacde876dee02b9386cf6c3baf134`다.
  이후 채택한 Nathan v0.7.8.1에 적용 가능한지는 미확인이다.

Strata 패치의 수동 적용 예시다. `PATCH_ROOT`는 이 `runtime` 디렉터리의 절대 경로,
`STRATA_SRC`는 별도로 받은 깨끗한 upstream checkout으로 지정한다.

```sh
git -C "$STRATA_SRC" checkout --detach 7695859aa13afb248bdb4d36f9b2e95707db079d
git -C "$STRATA_SRC" apply --check "$PATCH_ROOT/strata/evo-v0.1.43.patch"
git -C "$STRATA_SRC" apply "$PATCH_ROOT/strata/evo-v0.1.43.patch"
```

위 명령은 이 정리 작업에서 실행하지 않았다. 다른 revision은 별도 포팅 대상으로
취급한다. 패치 적용만으로 서비스 설정이나 실행 파일이 교체되지는 않는다.
가중치·expert pack·ROCm SDK·upstream checkout·컴파일 산출물은 별도 준비한다.

## 설정과의 관계

startup direct와 trim은 각각 `STRATA_STARTUP_DIRECT=1`,
`STRATA_STARTUP_TRIM_STAGING=1`로 선택한다. 전체 resident expert 모드 등 패치의
조건을 충족해야 동작한다. 진단용 copy/trim 검증 옵션은 일반 실행에서 끈다.
CPU projector mmap은 helper 실행 환경의 `STRATA_VISION_MMAP=1`로 선택한다.
on-demand vision은 서버의 vision 설정으로 선택하며 GPU encoder에는 지원되지 않는다.

`--pcie-mode direct --pcie-frac 1`, prompt checkpoint, KV 형식과 batch 크기는
기존 엔진의 설정 튜닝이다. 자체 GPU 커널 구현으로 계산하지 않는다.
startup direct가 GPU로의 모든 복사를 제거하는 것은 아니다. mmap도 접근한
파일 페이지에 물리 RAM을 사용한다.

## 저장 범위

패치, 공개 upstream 식별자, 개인 정보를 제거한 결과 요약만 보관한다.
credential, 네트워크 맵, 개인 메모, 서버 설정 원본, 쿠키, 대화, 원시 로그,
모델과 빌드 산출물은 제외한다. 원래 로컬 실험 기록은 그대로 보존하며
운영 서비스를 이 폴더로 자동 전환하지 않는다.

저작권 고지는 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)에 있다.
