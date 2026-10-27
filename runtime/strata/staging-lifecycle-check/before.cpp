
#include <cstdint>
#include <cstdio>
#include <mutex>
#include <memory>
#include <unordered_map>
#include <vector>
struct Source {
    std::mutex stage_mu_;
    std::unordered_map<int64_t,size_t> stage_of_;
    std::unordered_map<const uint8_t*,size_t> stage_idx_;
    std::vector<std::unique_ptr<uint8_t[]>> stage_buf_;
    std::vector<int64_t> stage_key_,stage_epoch_,stage_used_;
    std::vector<char> stage_busy_,stage_pf_,stage_ev_live_;
    std::vector<void*> stage_ev_;
    uint64_t stage_blob_=4096,stage_seq_=37;
    bool stage_grew_=true;
    size_t destroyed=0;
    void stage_free_events() {
        for(void*& e:stage_ev_) if(e) {++destroyed;e=nullptr;}
        stage_ev_.clear();stage_ev_live_.clear();stage_idx_.clear();
    }
    void add(bool busy=false) {
        size_t v=stage_buf_.size();
        stage_buf_.emplace_back(new uint8_t[stage_blob_]);
        stage_idx_[stage_buf_[v].get()]=v;stage_of_[v]=v;
        stage_key_.push_back(v);stage_epoch_.push_back(0);stage_used_.push_back(0);
        stage_busy_.push_back(busy);stage_pf_.push_back(0);
        stage_ev_.push_back(reinterpret_cast<void*>(v+1));stage_ev_live_.push_back(0);
    }
    bool release_startup_staging(uint64_t& released) {
        released = 0;
        std::lock_guard<std::mutex> lock(stage_mu_);
        for (char busy : stage_busy_) if (busy) return false;
        released = (uint64_t) stage_buf_.size() * stage_blob_;
        stage_of_.clear();
        stage_buf_.clear();
        stage_key_.clear();
        stage_epoch_.clear();
        stage_used_.clear();
        stage_busy_.clear();
        stage_seq_ = 0;
        stage_grew_ = false;
        return true;
    }
};
int main() {
    Source s;s.add();s.add();uint64_t bytes=0;
    bool first=s.release_startup_staging(bytes);
    bool empty=s.stage_idx_.empty() && s.stage_ev_.empty() && s.stage_ev_live_.empty() && s.stage_pf_.empty();
    bool reclaimed=first && bytes==8192 && s.stage_buf_.empty() && s.stage_seq_==0 && !s.stage_grew_;
    bool events=s.destroyed==2;
    s.add();
    bool aligned=s.stage_buf_.size()==s.stage_ev_.size() && s.stage_buf_.size()==s.stage_ev_live_.size() && s.stage_buf_.size()==s.stage_pf_.size() && s.stage_idx_.size()==1;
    s.stage_busy_[0]=1;size_t before=s.stage_buf_.size();size_t destroyed=s.destroyed;
    bool busy=!s.release_startup_staging(bytes) && bytes==0 && s.stage_buf_.size()==before && s.destroyed==destroyed;
    s.stage_busy_[0]=0;bool second=s.release_startup_staging(bytes) && bytes==4096;
    bool twice=s.release_startup_staging(bytes) && bytes==0;
    printf("{\"reclaimed\":%s,\"metadata_empty\":%s,\"events_destroyed\":%s,\"reallocation_aligned\":%s,\"busy_refused_without_mutation\":%s,\"second_release\":%s,\"idempotent_release\":%s}\n",
      reclaimed?"true":"false",empty?"true":"false",events?"true":"false",aligned?"true":"false",busy?"true":"false",second?"true":"false",twice?"true":"false");
}
