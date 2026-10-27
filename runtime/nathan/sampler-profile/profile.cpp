// Diagnostic interposer only. No logits, sampler state or RNG modification.
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <dlfcn.h>
#include <map>
#include <string>
#include <time.h>

struct common_sampler;
struct llama_context;
struct llama_sampler;
struct llama_token_data_array;
using llama_token = int32_t;

static uint64_t now(clockid_t clock) {
    timespec ts{}; clock_gettime(clock, &ts);
    return uint64_t(ts.tv_sec)*1000000000ULL + ts.tv_nsec;
}
struct time_pair { uint64_t wall = now(CLOCK_MONOTONIC), cpu = now(CLOCK_THREAD_CPUTIME_ID); };
struct sums {
    uint64_t calls=0, wall=0, cpu=0;
    void add(time_pair start) { ++calls; wall += now(CLOCK_MONOTONIC)-start.wall; cpu += now(CLOCK_THREAD_CPUTIME_ID)-start.cpu; }
};
struct stats { sums sample, sync; std::map<std::string,sums> samplers; uintptr_t ctx=0; };
static thread_local std::map<common_sampler*,stats> counters;
static thread_local stats* active=nullptr;
static thread_local int apply_depth=0;
template<class T> static T original(const char* name) {
    auto p=dlsym(RTLD_NEXT,name);
    if (!p) { std::fprintf(stderr,"EVO profile symbol absent: %s\n",name); std::abort(); }
    return reinterpret_cast<T>(p);
}
static void flush(common_sampler* smpl, const char* event) {
    auto i=counters.find(smpl);
    if (i==counters.end() || !i->second.sample.calls) return;
    const auto& s=i->second;
    std::fprintf(stderr,"EVO_SAMPLE_STATS {\"event\":\"%s\",\"sampler\":\"%p\",\"context\":\"%p\",\"samples\":%llu,\"wall_ns\":%llu,\"cpu_ns\":%llu,\"sync_calls\":%llu,\"sync_wall_ns\":%llu,\"sync_cpu_ns\":%llu,\"apply\":{",event,(void*)smpl,(void*)s.ctx,(unsigned long long)s.sample.calls,(unsigned long long)s.sample.wall,(unsigned long long)s.sample.cpu,(unsigned long long)s.sync.calls,(unsigned long long)s.sync.wall,(unsigned long long)s.sync.cpu);
    bool first=true;
    for(const auto& [name,t]:s.samplers) {
        std::fprintf(stderr,"%s\"%s\":{\"calls\":%llu,\"wall_ns\":%llu,\"cpu_ns\":%llu}",first?"":",",name.c_str(),(unsigned long long)t.calls,(unsigned long long)t.wall,(unsigned long long)t.cpu); first=false;
    }
    std::fprintf(stderr,"}}\n"); std::fflush(stderr); counters.erase(i);
}

llama_token common_sampler_sample(common_sampler* smpl,llama_context* ctx,int idx,bool grammar_first) {
    using Fn=llama_token(*)(common_sampler*,llama_context*,int,bool);
    static auto fn=original<Fn>("_Z21common_sampler_sampleP14common_samplerP13llama_contextib");
    auto& s=counters[smpl]; s.ctx=(uintptr_t)ctx;
    auto previous=active; active=&s; time_pair start;
    auto result=fn(smpl,ctx,idx,grammar_first);
    s.sample.add(start); active=previous; return result;
}
extern "C" void llama_synchronize(llama_context* ctx) {
    using Fn=void(*)(llama_context*);
    static auto fn=original<Fn>("llama_synchronize");
    if(!active) { fn(ctx); return; }
    time_pair start; fn(ctx); active->sync.add(start);
}
extern "C" void llama_sampler_apply(llama_sampler* sampler,llama_token_data_array* data) {
    using Fn=void(*)(llama_sampler*,llama_token_data_array*);
    using Name=const char*(*)(const llama_sampler*);
    static auto fn=original<Fn>("llama_sampler_apply");
    static auto name=original<Name>("llama_sampler_name");
    if(!active || !sampler) { fn(sampler,data); return; }
    std::string key=std::to_string(apply_depth)+":"+name(sampler);
    time_pair start; ++apply_depth; fn(sampler,data); --apply_depth;
    active->samplers[key].add(start);
}
void common_sampler_reset(common_sampler* smpl) {
    using Fn=void(*)(common_sampler*);
    static auto fn=original<Fn>("_Z20common_sampler_resetP14common_sampler");
    flush(smpl,"reset"); fn(smpl);
}
void common_sampler_free(common_sampler* smpl) {
    using Fn=void(*)(common_sampler*);
    static auto fn=original<Fn>("_Z19common_sampler_freeP14common_sampler");
    flush(smpl,"free"); fn(smpl);
}
