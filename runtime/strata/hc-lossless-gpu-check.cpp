#include "strata/core/hc_lossless.hpp"
#include "strata/kernels/fused_gr.hpp"
#include <cuda_runtime.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <random>
#include <vector>

#define OK(x) do { auto ec = (x); if (ec != cudaSuccess) { std::fprintf(stderr, "%d: %s\n", __LINE__, cudaGetErrorString(ec)); std::exit(2); } } while (0)
template<class T> T* gpu(size_t n) { T* p = nullptr; OK(cudaMalloc((void**)&p, n * sizeof(T))); return p; }
template<class T> void upload(T* p, const std::vector<T>& a) { OK(cudaMemcpy(p, a.data(), a.size() * sizeof(T), cudaMemcpyHostToDevice)); }
template<class T> std::vector<T> read(T* p, size_t n) { std::vector<T> a(n); OK(cudaMemcpy(a.data(), p, n * sizeof(T), cudaMemcpyDeviceToHost)); return a; }
template<class T> bool identical(const std::vector<T>& a, const std::vector<T>& b) { return a.size() == b.size() && std::memcmp(a.data(), b.data(), a.size() * sizeof(T)) == 0; }
uint16_t bf(float x) { uint32_t u; std::memcpy(&u, &x, 4); return (uint16_t)(u >> 16); }

int main(int argc, char** argv) {
    if (argc != 3) return 2;
    using namespace strata::kernels;
    constexpr int D = 10240, N = 2560, LR = 320, HC = 4, MAXT = 8;
    const size_t E = (size_t) D * LR;
    std::vector<uint16_t> host(E);
    std::ifstream input(argv[1], std::ios::binary);
    input.seekg((std::streamoff)std::strtoull(argv[2], nullptr, 10));
    input.read((char*)host.data(), E * 2);
    if (!input) return 2;
    strata::core::HcLossless enc(host);
    auto raw = gpu<uint16_t>(E), up = gpu<uint16_t>(E), inject = gpu<uint16_t>(D * HC);
    auto packed = gpu<uint8_t>(enc.packed.size());
    auto fallback = gpu<uint16_t>(std::max<size_t>(1, enc.fallback.size()));
    upload(raw, host); upload(packed, enc.packed);
    if (!enc.fallback.empty()) upload(fallback, enc.fallback);
    auto unpacked = gpu<uint16_t>(E);
    fused_gr_unpack_lossless(packed, fallback, unpacked, E, nullptr);
    OK(cudaDeviceSynchronize());
    if (!identical(host, read(unpacked, E))) return 3;
    OK(cudaFree(unpacked));

    // Exhaustive words plus mixed-exponent raw blocks on the actual GPU decoder.
    std::vector<uint16_t> all;
    for (unsigned i = 0; i < 65536; ++i) all.push_back((uint16_t)i);
    std::mt19937 rng(117);
    for (int i = 0; i < 65536; ++i) all.push_back((uint16_t)rng());
    strata::core::HcLossless every(all);
    auto ep = gpu<uint8_t>(every.packed.size()); auto ef = gpu<uint16_t>(every.fallback.size()); auto eo = gpu<uint16_t>(all.size());
    upload(ep, every.packed); upload(ef, every.fallback);
    fused_gr_unpack_lossless(ep, ef, eo, all.size(), nullptr);
    OK(cudaDeviceSynchronize());
    if (!identical(all, read(eo, all.size()))) return 3;
    OK(cudaFree(ep)); OK(cudaFree(ef)); OK(cudaFree(eo));

    std::uniform_real_distribution<float> f(-.05f, .05f);
    std::vector<uint16_t> hu(E), hi(D * HC);
    for (auto& v : hu) v = bf(f(rng));
    for (auto& v : hi) v = bf(f(rng));
    upload(up, hu); upload(inject, hi);
    std::vector<float> hr(MAXT * D), hn(D), hb(MAXT * N), hp(MAXT * HC);
    for (auto& v : hr) v = f(rng) * 20;
    for (auto& v : hn) v = 1 + f(rng);
    for (auto& v : hb) v = f(rng);
    for (auto& v : hp) v = f(rng);
    auto R = gpu<float>(hr.size()), norm = gpu<float>(D), bo = gpu<float>(hb.size()), prev = gpu<float>(hp.size());
    auto xn = gpu<float>(MAXT * D), lo = gpu<float>(MAXT * LR), rs = gpu<float>(MAXT * HC);
    auto inj = gpu<float>(MAXT * HC), mixed = gpu<float>(MAXT * N);
    auto q8 = gpu<uint8_t>(MAXT * N / 32 * 36); auto cnt = gpu<unsigned>(N / 32);
    upload(norm, hn); upload(bo, hb); upload(prev, hp);
    cudaStream_t st; OK(cudaStreamCreateWithFlags(&st, cudaStreamNonBlocking));
    OK(cudaDeviceSynchronize());
    size_t checks = 0, compared = 0;
    for (int T = 1; T <= MAXT; ++T) for (bool apply : {false, true})
    for (bool quant : {false, true}) for (bool graph : {false, true}) {
        std::vector<float> expected;
        std::vector<uint8_t> expected_q8;
        for (bool lossless : {false, true}) {
            FusedGrArgs args[MAXT];
            for (int t = 0; t < T; ++t) {
                auto& a = args[t]; a.R = R + t * D; a.R_out = R + t * D; a.apply = apply;
                a.bo_prev = bo + t * N; a.inj_prev = prev + t * HC; a.w_norm = norm;
                a.w_down = lossless ? nullptr : raw; a.w_up = up; a.w_inject = inject;
                a.lossless_down = lossless ? packed : nullptr; a.lossless_fallback = lossless ? fallback : nullptr;
                a.lo = lo + t * LR; a.rs = rs + t * HC; a.inject_out = inj + t * HC; a.mixed = mixed + t * N;
                if (quant) { a.q8_mixed = q8 + t * (N / 32) * 36; a.q8_cnt = cnt; }
            }
            auto call = [&] { return fused_gr_read_multi(args, T, xn, st); };
            upload(R, hr); OK(cudaMemset(cnt, 0, N / 32 * sizeof(unsigned))); OK(cudaDeviceSynchronize());
            const bool wrote_q8 = call(); OK(cudaStreamSynchronize(st));
            upload(R, hr); OK(cudaMemset(cnt, 0, N / 32 * sizeof(unsigned))); OK(cudaDeviceSynchronize());
            if (graph) {
                cudaGraph_t g; cudaGraphExec_t ge;
                OK(cudaStreamBeginCapture(st, cudaStreamCaptureModeThreadLocal));
                call(); OK(cudaStreamEndCapture(st, &g));
                OK(cudaGraphInstantiate(&ge, g, nullptr, nullptr, 0));
                OK(cudaGraphLaunch(ge, st)); OK(cudaStreamSynchronize(st));
                OK(cudaGraphExecDestroy(ge)); OK(cudaGraphDestroy(g));
            } else { call(); OK(cudaStreamSynchronize(st)); }
            std::vector<float> result;
            for (auto [p, n] : {std::pair{R, T * D}, {lo, T * LR}, {rs, T * HC}, {inj, T * HC}, {mixed, T * N}}) {
                auto part = read(p, n); result.insert(result.end(), part.begin(), part.end());
            }
            if (!std::all_of(result.begin(), result.end(), [](float x) { return std::isfinite(x); })) return 4;
            auto qr = wrote_q8 ? read(q8, T * (N / 32) * 36) : std::vector<uint8_t>{};
            if (!lossless) { expected = result; expected_q8 = qr; }
            else {
                if (!identical(expected, result) || !identical(expected_q8, qr)) {
                    std::fprintf(stderr, "MISMATCH T=%d apply=%d quant=%d graph=%d\n", T, apply, quant, graph); return 5;
                }
                ++checks; compared += result.size();
            }
        }
    }
    std::printf("PASS variant=%d fused_cases=%zu float_words=%zu bf16_words=%zu all_words=131072 packed=%zu fallback=%zu\n",
                fused_gr_variant(), checks, compared, host.size(), enc.packed.size(), enc.fallback.size() * 2);
    OK(cudaStreamDestroy(st));
    return 0;
}
