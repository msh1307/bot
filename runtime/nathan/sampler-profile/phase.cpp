#include "profile.cpp"
#include <cstring>
#include <vector>
struct common_speculative;
struct llama_batch;
struct common_speculative_token_dist;

static void phase_log(const char* name,int tokens,time_pair start) {
    const auto wall=now(CLOCK_MONOTONIC)-start.wall;
    const auto cpu=now(CLOCK_THREAD_CPUTIME_ID)-start.cpu;
    std::fprintf(stderr,"EVO_PHASE {\"name\":\"%s\",\"tokens\":%d,\"wall_ns\":%llu,\"cpu_ns\":%llu}\n",name,tokens,(unsigned long long)wall,(unsigned long long)cpu);
}
bool common_speculative_process(common_speculative* spec,const llama_batch& batch) {
    using Fn=bool(*)(common_speculative*,const llama_batch&);
    static auto fn=original<Fn>("_Z26common_speculative_processP18common_speculativeRK11llama_batch");
    // Pinned llama.h: llama_batch's first member is int32_t n_tokens.
    int32_t tokens; std::memcpy(&tokens,&batch,sizeof(tokens));
    time_pair start;auto result=fn(spec,batch);phase_log("inject",tokens,start);return result;
}
void common_speculative_draft(common_speculative* spec) {
    using Fn=void(*)(common_speculative*);
    static auto fn=original<Fn>("_Z24common_speculative_draftP18common_speculative");
    time_pair start;fn(spec);phase_log("draft",0,start);
}

std::vector<llama_token> common_sampler_sample_and_accept_n(common_sampler* smpl,llama_context* ctx,
        const std::vector<int>& indices,const std::vector<llama_token>& draft,
        const std::vector<common_speculative_token_dist>& distributions,bool grammar_first) {
    using Fn=std::vector<llama_token>(*)(common_sampler*,llama_context*,const std::vector<int>&,
        const std::vector<llama_token>&,const std::vector<common_speculative_token_dist>&,bool);
    static auto fn=original<Fn>("_Z34common_sampler_sample_and_accept_nP14common_samplerP13llama_contextRKSt6vectorIiSaIiEES7_RKS3_I29common_speculative_token_distSaIS8_EEb");
    time_pair start;auto result=fn(smpl,ctx,indices,draft,distributions,grammar_first);
    phase_log("verify_sample",result.size(),start);return result;
}
