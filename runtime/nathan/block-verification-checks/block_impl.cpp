static std::vector<llama_token> evo_sample_block(
        common_sampler * gsmpl, llama_context * ctx, const std::vector<int> & idxs,
        const llama_tokens & draft, const std::vector<common_speculative_token_dist> & dists, bool grammar_first = false) {
    std::unique_ptr<common_sampler, decltype(&common_sampler_free)> preview(common_sampler_clone(gsmpl), common_sampler_free);
    std::vector<llama_token> result;
    double weight = 1.0;
    for (size_t i = 0; i <= draft.size(); ++i) {
        common_sampler_sample(preview.get(), ctx, idxs[i], i < draft.size() ? true : grammar_first);
        auto * p = common_sampler_get_candidates(preview.get(), false);
        std::unordered_map<llama_token, double> q;
        double qsum = 0.0, psum = 0.0;
        if (i < draft.size()) {
            GGML_ASSERT(dists[i].ids.size() == dists[i].probs.size());
            for (float value : dists[i].probs) qsum += value;
            GGML_ASSERT(qsum > 0.0);
            for (size_t j = 0; j < dists[i].ids.size(); ++j) q[dists[i].ids[j]] += dists[i].probs[j] / qsum;
        }
        for (size_t j = 0; j < p->size; ++j) psum += p->data[j].p;
        GGML_ASSERT(psum > 0.0);
        std::vector<double> residual(p->size + 1);
        double pd = 0.0, total = 0.0;
        for (size_t j = 0; j < p->size; ++j) {
            const auto id = p->data[j].id;
            const auto it = q.find(id);
            const double pv = p->data[j].p / psum;
            if (i < draft.size() && id == draft[i]) pd = pv;
            residual[j] = std::max(0.0, weight * pv - (it == q.end() ? 0.0 : it->second));
            total += residual[j];
        }
        residual.back() = 1.0 - weight;
        total += residual.back();
        if (total > 0.0) {
            std::discrete_distribution<size_t> choose(residual.begin(), residual.end());
            const size_t selected = choose(gsmpl->speculative_rng);
            if (selected < p->size) {
                result.assign(draft.begin(), draft.begin() + i);
                result.push_back(p->data[selected].id);
            }
        }
        if (i == draft.size()) break;
        const auto it = q.find(draft[i]);
        GGML_ASSERT(it != q.end() && it->second > 0.0);
        weight = std::min(1.0, weight * pd / it->second);
        if (weight == 0.0) break;
        common_sampler_accept(preview.get(), draft[i], true);
    }
    GGML_ASSERT(!result.empty());
    const llama_vocab * vocab = llama_model_get_vocab(llama_get_model(ctx));
    for (size_t i = 0; i < result.size(); ++i) {
        common_sampler_sample(gsmpl, ctx, idxs[i], i < draft.size() ? true : grammar_first);
        common_sampler_accept(gsmpl, result[i], true);
        if (llama_vocab_is_eog(vocab, result[i])) {
            result.resize(i + 1);
            break;
        }
    }
    return result;
}

