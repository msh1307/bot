#include "strata/core/hc_lossless.hpp"
#include <cassert>
#include <cstdio>
#include <random>

int main() {
    std::vector<uint16_t> v;
    // Every possible BF16 word, including zero, signs, subnormals, infinity and NaN payloads.
    for (unsigned i = 0; i < 65536; ++i) v.push_back((uint16_t) i);
    // Blocks covering the exponent range boundary and mandatory raw fallback.
    for (unsigned base = 0; base < 256; ++base)
        for (unsigned span : {0U, 1U, 15U, 16U, 255U})
            for (unsigned j = 0; j < 64; ++j)
                v.push_back((uint16_t) ((((base + j % (span + 1)) % 256) << 7) | (j * 19 % 128) | ((j % 2) << 15)));
    std::mt19937 rng(117);
    for (int i = 0; i < 1048576; ++i) v.push_back((uint16_t) rng());
    strata::core::HcLossless enc(v);
    for (size_t i = 0; i < v.size(); ++i) assert(enc.at(i) == v[i]);
    for (size_t n : {0, 1, 63, 65}) {
        bool rejected = false;
        try { strata::core::HcLossless bad(std::vector<uint16_t>(n, 0)); }
        catch (const std::runtime_error&) { rejected = true; }
        assert(rejected);
    }
    std::printf("exact_words=%zu packed_bytes=%zu fallback_bytes=%zu invalid_lengths_rejected=4\n", v.size(), enc.packed.size(), enc.fallback.size() * 2);
}
