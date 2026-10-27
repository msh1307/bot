// Experimental IQ2 pair-LUT microbenchmark, not a production kernel.
// Reference arithmetic follows Strata 7dd5cf1 Split<16/17/22> and row_dot_80_sub16.
// Both variants omit the fused gate/up activation epilogue. No model TPS claim.
#include <cuda_runtime.h>
#include <cuda_fp16.h>
#define GGML_COMMON_DECL_CUDA
#define GGML_COMMON_IMPL_CUDA
#include "ggml-common.h"
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <random>
#include <vector>

static void ck(cudaError_t e) {
    if (e != cudaSuccess) { std::fprintf(stderr,"GPU: %s\n",cudaGetErrorString(e)); std::exit(2); }
}
template<class T> T* alloc(size_t n) { T* p;ck(cudaMalloc((void**)&p,n*sizeof(T)));return p; }
constexpr int R=12800, NX=80, NP=1280;
template<int TY> struct Traits;
template<> struct Traits<16> { static constexpr int B=66,N=256; };
template<> struct Traits<17> { static constexpr int B=74,N=512; };
template<> struct Traits<22> { static constexpr int B=82,N=1024; };
template<int TY> __device__ const uint64_t* grid() {
    if constexpr(TY==16)return iq2xxs_grid;
    if constexpr(TY==17)return iq2xs_grid;
    if constexpr(TY==22)return iq2s_grid;
}
__device__ __forceinline__ unsigned signs7(unsigned v) {
    v&=127; return v^((__popc(v)&1)<<7);
}
__device__ __forceinline__ int get32(const void* p) {return *(const int*)p;}
template<int TY> __global__ void maps(uint16_t* out) {
    int i=blockIdx.x*blockDim.x+threadIdx.x;if(i>=Traits<TY>::N)return;
    uint64_t g=grid<TY>()[i];unsigned m=0;
    for(int j=0;j<4;++j) {
        int a=(g>>(j*16))&255,b=(g>>(j*16+8))&255;
        a=(a==8?0:a==25?1:2);b=(b==8?0:b==25?1:2);
        m|=(a*3+b)<<(4*j);
    }
    out[i]=m;
}
template<int NC> __global__ void build(const block_q8_1* x,uint32_t* tab) {
    int i=blockIdx.x*blockDim.x+threadIdx.x;if(i>=NC*NP*9)return;
    int m=i%9,p=(i/9)%NP,c=i/(9*NP),a=m/3,b=m%3;
    const int mags[3]={8,25,43};
    int u=x[c*NX+p/16].qs[(p%16)*2],v=x[c*NX+p/16].qs[(p%16)*2+1];
    int plus=mags[a]*u+mags[b]*v,minus=mags[a]*u-mags[b]*v;
    tab[i]=uint16_t(plus)|(uint32_t(uint16_t(minus))<<16);
}
template<bool LUT> struct W {
    int g[8];unsigned ids[4],sgn[4];int ls0,ls1;float d;
};
template<int TY,bool LUT> __device__ __forceinline__ W<LUT> load(
        const uint8_t* row,int k,const uint64_t* sg,const uint16_t* sm) {
    const uint8_t* b=row+(k/8)*Traits<TY>::B;int q=k%8;
    W<LUT> w;w.d=__half2float(*(const __half*)b);
    uint32_t aux=0;
    if constexpr(TY==16) {aux=*(const uint32_t*)(b+2+8*q+4);w.ls0=(aux>>27)|1;w.ls1=0;}
    else {uint8_t sc=b[(TY==17?66:74)+q];w.ls0=sc&15;w.ls1=sc>>4;}
    #pragma unroll
    for(int g=0;g<4;++g) {
        unsigned idx,s;
        if constexpr(TY==16) {idx=b[2+8*q+g];s=signs7(aux>>(7*g));}
        if constexpr(TY==17) {unsigned v=((const uint16_t*)(b+2))[q*4+g];idx=v&511;s=signs7(v>>9);}
        if constexpr(TY==22) {idx=b[2+q*4+g]|(((b[66+q]>>(g*2))&3)<<8);s=b[2+32+q*4+g];}
        if constexpr(LUT) {w.ids[g]=sm[idx];w.sgn[g]=s;}
        else {
            uint2 v=((const uint2*)sg)[idx];unsigned ss=s*0x01010101u;
            int s0=__vcmpne4(ss&0x08040201u,0),s1=__vcmpne4(ss&0x80402010u,0);
            w.g[g*2]=__vsub4(v.x^s0,s0);w.g[g*2+1]=__vsub4(v.y^s1,s1);
        }
    }
    return w;
}
template<int TY,bool LUT> __device__ __forceinline__ float apply(
        const W<LUT>& w,const block_q8_1* x,const uint32_t* tab,int k) {
    int s0=0,s1=0;
    #pragma unroll
    for(int g=0;g<4;++g) {
        int v=0;
        if constexpr(LUT) {
            #pragma unroll
            for(int j=0;j<4;++j) {
                unsigned m=(w.ids[g]>>(4*j))&15;
                unsigned signs=(w.sgn[g]>>(2*j))&3;
                uint32_t packed=tab[((k*16+g*4+j)*9)+m];
                int z=int(int16_t(packed>>(((signs^(signs>>1))&1)*16)));
                v+=(signs&1)?-z:z;
            }
        } else {
            v=__dp4a(w.g[g*2],get32(x[k].qs+g*8),0);
            v=__dp4a(w.g[g*2+1],get32(x[k].qs+g*8+4),v);
        }
        if constexpr(TY==16)s0+=v;
        else {if(g<2)s0+=v;else s1+=v;}
    }
    int sum;
    if constexpr(TY==16)sum=s0*w.ls0/8;
    else sum=(s0*w.ls0+s1*w.ls1+(s0+s1)/2)/4;
    float d=w.d*__low2float(x[k].ds);
    return d*sum;
}
template<int TY,bool LUT,int NC> __global__ void matvec(
        const uint8_t* weights,const block_q8_1* x,const uint32_t* tab,
        const uint16_t* map,float* out) {
    __shared__ uint64_t sg[LUT?1:Traits<TY>::N];
    __shared__ uint16_t sm[LUT?Traits<TY>::N:1];
    for(int i=threadIdx.x;i<Traits<TY>::N;i+=blockDim.x) {
        if constexpr(LUT)sm[i]=map[i];else sg[i]=grid<TY>()[i];
    }
    __syncthreads();
    int row=blockIdx.x*16+threadIdx.x/16,t=threadIdx.x%16;if(row>=R)return;
    const uint8_t* w=weights+row*Traits<TY>::B*10;
    float a[NC]={},b[NC]={};
    for(int k=t;k<80;k+=32) {
        auto v=load<TY,LUT>(w,k,sg,sm);
        #pragma unroll
        for(int c=0;c<NC;++c)a[c]+=apply<TY,LUT>(v,x+c*NX,tab+c*NP*9,k);
    }
    for(int k=t+16;k<64;k+=32) {
        auto v=load<TY,LUT>(w,k,sg,sm);
        #pragma unroll
        for(int c=0;c<NC;++c)b[c]+=apply<TY,LUT>(v,x+c*NX,tab+c*NP*9,k);
    }
    #pragma unroll
    for(int c=0;c<NC;++c) {
        float s=__fadd_rn(a[c],b[c]);
        #pragma unroll
        for(int d=8;d>0;d>>=1)s+=__shfl_xor_sync(0xffffffffu,s,d);
        if(t==0)out[c*R+row]=s;
    }
}
template<int TY,int NC> void test(const char* filename) {
    std::vector<uint8_t> hw(R*Traits<TY>::B*10);
    std::ifstream file(filename,std::ios::binary);file.read((char*)hw.data(),hw.size());
    if((size_t)file.gcount()!=hw.size()){std::fprintf(stderr,"bad weights\n");std::exit(2);}
    auto w=alloc<uint8_t>(hw.size());ck(cudaMemcpy(w,hw.data(),hw.size(),cudaMemcpyHostToDevice));
    auto x=alloc<block_q8_1>(NC*NX);auto tab=alloc<uint32_t>(NC*NP*9);
    auto map=alloc<uint16_t>(Traits<TY>::N);auto ref=alloc<float>(NC*R),out=alloc<float>(NC*R);
    maps<TY><<<(Traits<TY>::N+255)/256,256>>>(map);
    std::mt19937 rng(1307);size_t mismatches=0;
    std::vector<float> hr(NC*R),ho(NC*R);
    for(int trial=0;trial<4;++trial) {
        std::vector<block_q8_1> hx(NC*NX);
        for(auto& b:hx) {b.ds=__floats2half2_rn(.001f+(rng()%4096)*.0005f,0);
            for(auto& q:b.qs)q=int(rng()%255)-127;}
        ck(cudaMemcpy(x,hx.data(),sizeof(block_q8_1)*hx.size(),cudaMemcpyHostToDevice));
        build<NC><<<(NC*NP*9+255)/256,256>>>(x,tab);
        matvec<TY,false,NC><<<(R+15)/16,256>>>(w,x,tab,map,ref);
        matvec<TY,true,NC><<<(R+15)/16,256>>>(w,x,tab,map,out);
        ck(cudaDeviceSynchronize());
        ck(cudaMemcpy(hr.data(),ref,hr.size()*4,cudaMemcpyDeviceToHost));
        ck(cudaMemcpy(ho.data(),out,ho.size()*4,cudaMemcpyDeviceToHost));
        for(size_t i=0;i<hr.size();++i)if(std::memcmp(&hr[i],&ho[i],4))++mismatches;
    }
    auto launch=[&](int mode) {
        if(mode==1)build<NC><<<(NC*NP*9+255)/256,256>>>(x,tab);
        if(mode==0)matvec<TY,false,NC><<<(R+15)/16,256>>>(w,x,tab,map,ref);
        else matvec<TY,true,NC><<<(R+15)/16,256>>>(w,x,tab,map,out);
    };
    cudaEvent_t a,b;ck(cudaEventCreate(&a));ck(cudaEventCreate(&b));
    std::vector<float> times[3];
    for(int round=0;round<9;++round)for(int j=0;j<3;++j) {
        int mode=(round%2)?2-j:j;
        for(int i=0;i<4;++i)launch(mode);
        ck(cudaEventRecord(a));for(int i=0;i<25;++i)launch(mode);ck(cudaEventRecord(b));
        ck(cudaEventSynchronize(b));float ms;ck(cudaEventElapsedTime(&ms,a,b));
        times[mode].push_back(ms*1000/25);
    }
    for(auto& ts:times)std::sort(ts.begin(),ts.end());
    std::printf("{\"type\":%d,\"tokens\":%d,\"compared_float_outputs\":%d,\"bit_mismatches\":%zu,"
        "\"direct_us\":%.4f,\"lut_including_build_us\":%.4f,\"lut_consume_only_us\":%.4f,"
        "\"table_bytes\":%d}\n",TY,NC,4*NC*R,mismatches,times[0][4],times[1][4],times[2][4],NC*NP*9*4);
    std::fflush(stdout);
    ck(cudaEventDestroy(a));ck(cudaEventDestroy(b));
    for(void* p:{(void*)w,(void*)x,(void*)tab,(void*)map,(void*)ref,(void*)out})ck(cudaFree(p));
    if(mismatches)std::exit(3);
}
int main(int argc,char** argv) {
    if(argc!=4)return 2;
    test<16,1>(argv[1]);test<16,4>(argv[1]);
    test<17,1>(argv[2]);test<17,4>(argv[2]);
    test<22,1>(argv[3]);test<22,4>(argv[3]);
}
