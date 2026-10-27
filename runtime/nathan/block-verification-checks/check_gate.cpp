#include <algorithm>
#include <cassert>
#include <vector>
#include <cstdio>
struct params_t { float temp=1; int mirostat=0; float xtc_probability=0,adaptive_target=-1; bool backend_sampling=false; std::vector<int> samplers{}; };
struct common_sampler { void* grmr=nullptr; params_t params; };
constexpr int COMMON_SAMPLER_TYPE_ADAPTIVE_P=9;
bool supported(common_sampler* gsmpl) {
    const bool block_supported = gsmpl->grmr == nullptr &&
        gsmpl->params.temp > 0.0f && gsmpl->params.mirostat == 0 &&
        gsmpl->params.xtc_probability == 0.0f && gsmpl->params.adaptive_target < 0.0f &&
        !gsmpl->params.backend_sampling &&
        std::find(gsmpl->params.samplers.begin(), gsmpl->params.samplers.end(),
                  COMMON_SAMPLER_TYPE_ADAPTIVE_P) == gsmpl->params.samplers.end();
return block_supported;
}
int main() {
 common_sampler s; assert(supported(&s)); int checked=1;
 auto no=[&](common_sampler x){assert(!supported(&x));++checked;};
 auto t=s;t.grmr=&s;no(t);
 t=s;t.params.temp=0;no(t);t.params.temp=-1;no(t);
 t=s;t.params.mirostat=1;no(t);t.params.mirostat=2;no(t);
 t=s;t.params.xtc_probability=.1f;no(t);
 t=s;t.params.adaptive_target=.5f;no(t);
 t=s;t.params.backend_sampling=true;no(t);
 t=s;t.params.samplers={9};no(t);
 t=s;t.params.samplers={0,1,2};assert(supported(&t));++checked;
 printf("{\"supported_gate_cases\":%d,\"passed\":true}\n",checked);
}
