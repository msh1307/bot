#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <memory>
#include <random>
#include <unordered_map>
#include <vector>
#define GGML_ASSERT assert
using llama_token = int;
using llama_tokens = std::vector<int>;
struct llama_token_data { int id; float p; };
struct llama_token_data_array { llama_token_data *data; size_t size; };
struct common_speculative_token_dist { std::vector<int> ids; std::vector<float> probs; };
struct llama_context { std::vector<std::vector<llama_token_data>> rows; };
struct llama_vocab {};
struct common_sampler {
    std::mt19937 speculative_rng;
    std::vector<int> history;
    std::vector<llama_token_data> cur;
    llama_token_data_array arr{};
};
static common_sampler *common_sampler_clone(common_sampler *s) { return new common_sampler(*s); }
static void common_sampler_free(common_sampler *s) { delete s; }
static int common_sampler_sample(common_sampler *s, llama_context *ctx, int idx, bool grammar_first) {
    if (idx+1==int(ctx->rows.size())) assert(!grammar_first); else assert(grammar_first);
    assert(int(s->history.size())==idx);
    s->cur=ctx->rows[idx];s->arr={s->cur.data(),s->cur.size()};return 0;
}
static llama_token_data_array *common_sampler_get_candidates(common_sampler *s,bool) {return &s->arr;}
static void common_sampler_accept(common_sampler *s,int token,bool) {s->history.push_back(token);}
static const llama_context *llama_get_model(llama_context *ctx) {return ctx;}
static const llama_vocab *llama_model_get_vocab(const llama_context *) {static llama_vocab v;return &v;}
static bool llama_vocab_is_eog(const llama_vocab *,int token) {return token==2;}
#include "block_impl.cpp"

int main() {
    std::mt19937 random(182);double max_sigma=0;int checked=0;
    for(int width: {3,6,8}) for(int scenario=0;scenario<12;scenario++) {
        const int rounds=150000, nodes=(1<<(width+1))-1, bins=1<<(width+1);
        std::vector<double> p(nodes),q(nodes);
        for(int i=0;i<nodes;i++) {
            p[i]=.05+.9*(random()%1001)/1000.;q[i]=.05+.9*(random()%1001)/1000.;
            if(scenario==0)q[i]=p[i];
        }
        std::vector<int> counts(bins);
        std::vector<int> idxs(width+1);
        for(int i=0;i<=width;i++)idxs[i]=i;
        for(int trial=0;trial<rounds;trial++) {
            int node=0;llama_tokens draft;llama_context ctx;std::vector<common_speculative_token_dist> dists;
            for(int i=0;i<=width;i++) {
                ctx.rows.push_back({{0,float(p[node])},{1,float(1-p[node])}});
                if(i<width) {
                    dists.push_back({{0,1},{float(q[node]),float(1-q[node])}});
                    int token=std::generate_canonical<double,53>(random)<q[node]?0:1;
                    draft.push_back(token);node=2*node+1+token;
                }
            }
            common_sampler s; s.speculative_rng.seed(random());
            auto before=s;
            auto output=evo_sample_block(&s,&ctx,idxs,draft,dists);
            auto replay=evo_sample_block(&before,&ctx,idxs,draft,dists);
            assert(output==replay && s.history==output);
            node=0;int code=0;
            for(int token:output) {code=2*code+token;node=2*node+1+token;}
            while(output.size()<size_t(width+1)) {
                int token=std::generate_canonical<double,53>(random)<p[node]?0:1;
                code=2*code+token;node=2*node+1+token;output.push_back(token);
            }
            counts[code]++;
        }
        for(int code=0;code<bins;code++) {
            double expected=1;int node=0;
            for(int i=width;i>=0;i--) {int t=(code>>i)&1;expected*=t?1-p[node]:p[node];node=2*node+1+t;}
            double sigma=std::abs(counts[code]-rounds*expected)/std::sqrt(rounds*expected*(1-expected));
            if(rounds*expected>=5)max_sigma=std::max(max_sigma,sigma);
            assert(std::abs(counts[code]-rounds*expected) < 7*std::sqrt(rounds*expected*(1-expected))+7);checked++;
        }
    }
    llama_context eos; eos.rows={{{2,1.0f}},{{0,1.0f}},{{0,1.0f}}};
    common_sampler s;s.speculative_rng.seed(9);
    auto result=evo_sample_block(&s,&eos,{0,1,2},{2,0},{{{2},{1.0f}},{{0},{1.0f}}});
    assert(result==std::vector<int>{2} && s.history==result);
    std::printf("{\"samples\":5400000,\"draft_widths\":[3,6,8],\"joint_bins\":%d,\"maximum_sigma\":%.9g,\"seeded_replay\":true,\"committed_history\":true,\"eos_truncation\":true}\n",checked,max_sigma);
}
