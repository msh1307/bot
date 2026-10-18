# 과거 실측 요약

EVO-X2 64GB / gfx1151 / 75W의 기존 기록을 개인 정보 없이 요약했다.
2026-10-18의 디렉터리 정리에서 다시 측정한 결과는 아니다.

## 직접 만든 변경

| 변경 | 기존 측정 | 해석 한계 |
|---|---|---|
| Flash startup direct I/O | 로더 cgroup peak 28.848 → 20.949GiB, 로딩 34.574 → 28.449초, 추가 시스템 swap-out 409.8125MiB → 48KiB | 동일 비교의 swappiness 60 조건. 회수 가능한 파일 캐시를 포함하므로 상시 가용 RAM 7.899GiB 증가를 뜻하지 않음 |
| Flash staging 반환 | CPU 조립 버퍼 594,048,000bytes, 566.53MiB 반환 | 반환량과 OS MemAvailable 변화는 다를 수 있음. 새 throughput 개선으로 주장하지 않음 |
| 정상 trim 재로딩 3회 | 27.284 / 27.283 / 27.279초, 추가 시스템 swap-out 4 / 4 / 0KiB | 모든 cold/warm 로딩의 swap 0 보장은 아님 |
| 정상 trim의 긴 입력 | 120,000 입력 + 128 출력, prefill 430.4tok/s, 생성 38.1tok/s, 최소 가용 RAM 8.636GiB, 추가 swap-out 0 | 짧은 생성 표본이며 128K 전체 깊은 추론 시험은 아님 |
| CPU projector mmap | 334개 weight tensor, 865.48MiB를 파일 매핑에 연결해 별도 익명 weight 복사 생략 | 파일 페이지와 encoding 버퍼는 RAM을 사용함. 865.48MiB 전체 절감으로 환산하지 않음 |

Strata v0.1.43 포팅에는 기존 변경을 보존했다. 위 loader/trim 비교는 포팅 전
기록이며 v0.1.43 전체 패치의 새로운 A/B 수치가 아니다. zero swap은
OS 전체 SSD write 0을 뜻하지 않는다.

## 설정 튜닝과 외부 런타임 채택

- Flash의 기존 mapped 경로에서 `--pcie-mode direct --pcie-frac 1` 조합은
  짧은 수학 생성 48.8 → 54.1tok/s, 22,604-token 첫 입력 뒤 생성
  44.2 → 49.3tok/s, cached 생성 46.5 → 51.0tok/s였다.
  짧은 출력·thinking off의 제한된 비교다. 자체 GPU 커널 성과가 아니며,
  direct만 선택하고 GPU 분담을 바꾸지 않은 arm에서는 개선되지 않았다.
- 27B는 RANA Q6_K와 DFlash2 Q4_K_M을 유지하고 외부 Nathan v0.7.8.1
  Vulkan 런타임과 batch/ubatch 2048을 채택했다. 같은 16K fixture에서 영어 코드
  중앙값 17.23 → 18.78tok/s, 한국어 thinking 21.05 → 21.94tok/s,
  full prefill 65.3–65.7 → 53.5–53.6초였다. fixture별 두 표본이며
  최저 20tok/s 보장이나 모델 정확도 향상 근거가 아니다.
- Prompt checkpoint와 대화 전체 KV 주차는 다른 기능이다. 체크포인트 수를
  독립 대화 보관 개수나 항상 빨라지는 배수로 해석하지 않는다.

## 미채택·미검증

| 후보 | 결과 | 결정 |
|---|---|---|
| Adaptive DFlash2 3–7 | 영어 코드 중앙값 16.44, 한국어 thinking 20.40tok/s; fixed 3은 17.24 / 21.04 | 회귀로 미채택 |
| Adaptive DFlash2 1–7 | 16.73 / 20.12tok/s | 회귀로 미채택 |
| PLE row cache 1M → 2M | 120K 요청 NVMe read 2.8281 → 2.8206GiB, 생성 양쪽 36.7tok/s, RAM 여유 약 115MiB 감소 | 이득 작아 미채택 |
| Fused prefill | 466.0 → 644.1tok/s, 코드 생성 44.7 → 43.0tok/s; 별도 깊은 수학의 CPU peak 86.4°C | 기본 미채택 |
| EngramHalo / Gufo 엔진 전환 | 소스 비교 중심. 현 모델의 동일 조건 실행 A/B 미완료 | 성능 개선으로 집계하지 않음 |

후속 비교 시 모델·quant·전력·sampler·KV·입력 길이·출력 한도·cache 상태를
기록하고 prefill, decode, 로딩 peak, 추가 swap-out, NVMe read/write를 분리한다.
