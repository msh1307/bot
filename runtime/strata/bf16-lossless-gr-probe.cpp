/*
 * Isolated gfx1151 HC-down benchmark, not a production runtime feature.
 * Kernel arithmetic from Niko1221/Strata, revision
 * 7dd5cf18e68c639a8773e096c5c79f971ea2a9f5, fused_gr.cu.
 * Experimental lossless weight decoder added for EVO-X2 research.
 * CLI: probe GGUF ABSOLUTE_BYTE_OFFSET 10240 320
 * Only this tested shape is supported. See runtime/manifest.json.
 *
 * MIT License
 * Copyright (c) 2026 Niko1221 and the Strata contributors
 * 
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 * 
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 * 
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

#include <hip/hip_runtime.h>
#include "strata/kernels/fused_gr.hpp"
#include "strata/kernels/bf16_bits.hpp"
#include "s26_tsum.cuh"
#include <vector>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <algorithm>
#include <cstdint>
#define OK(x) do { auto ec=(x); if(ec!=hipSuccess){fprintf(stderr,"HIP %s:%d: %s\n",__FILE__,__LINE__,hipGetErrorString(ec));exit(2);} }while(0)

namespace strata::kernels {
constexpr int N=2560,HC=4,D=10240,LR=320,THREADS=256,WARPS=8,DOWN_BLOCKS=40;
struct GrMulti { FusedGrArgs a[kFusedGrMaxT]; float* xn; int T; const uint8_t* packed; const uint16_t* fallback; };
__device__ __forceinline__ float warp_sum(float v) {
 for(int off=16;off>0;off>>=1)v+=__shfl_xor_sync(0xffffffffu,v,off,32);return v;
}
struct Bf16x8 { float4 a, b; };
__device__ __forceinline__ Bf16x8 unpack8(const uint4 w) {
    return {
        make_float4(__uint_as_float(w.x << 16), __uint_as_float(w.x & 0xffff0000u),
                    __uint_as_float(w.y << 16), __uint_as_float(w.y & 0xffff0000u)),
        make_float4(__uint_as_float(w.z << 16), __uint_as_float(w.z & 0xffff0000u),
                    __uint_as_float(w.w << 16), __uint_as_float(w.w & 0xffff0000u))
    };
}
__device__ __forceinline__ float dot8u(const Bf16x8& w, const float4 x0, const float4 x1) {
    float acc = 0.0f;
    acc = fmaf(w.a.x, x0.x, acc);
    acc = fmaf(w.a.y, x0.y, acc);
    acc = fmaf(w.a.z, x0.z, acc);
    acc = fmaf(w.a.w, x0.w, acc);
    acc = fmaf(w.b.x, x1.x, acc);
    acc = fmaf(w.b.y, x1.y, acc);
    acc = fmaf(w.b.z, x1.z, acc);
    acc = fmaf(w.b.w, x1.w, acc);
    return acc;
}
__device__ __forceinline__ float dot8u_ptr(const Bf16x8& w, const float* x) {
    const float4* x4 = reinterpret_cast<const float4*>(x);
    return dot8u(w, x4[0], x4[1]);
}

// 8 bf16 packed in a uint4 against 8 16-byte-aligned floats (same 8-FMA order).
__device__ __forceinline__ float dot8(const uint4 w, const float* x) {
    return dot8u_ptr(unpack8(w), x);
}


template<bool Q8> __device__ __forceinline__ uint4 ldw(const uint16_t* w,const float*,size_t e){ return __ldg(reinterpret_cast<const uint4*>(w+e)); }
template<bool Q8> __device__ __forceinline__ float dotw(bool,const uint4 w,const float*x){return dot8(w,x);}
__device__ __forceinline__ uint4 lossless8(const uint8_t* packed,const uint16_t* fallback,size_t e){
 const uint8_t*p=packed+(e/64)*100;const uint32_t h=*reinterpret_cast<const uint32_t*>(p);const int j=e%64;
 if(h&0x80000000u)return __ldg(reinterpret_cast<const uint4*>(fallback+(size_t)(h&0x7fffffffu)*64+j));
 const uint2 lo=make_uint2(*reinterpret_cast<const uint32_t*>(p+4+j),*reinterpret_cast<const uint32_t*>(p+8+j));
 const uint32_t de=*reinterpret_cast<const uint32_t*>(p+68+j/2);
 uint32_t words[4];
 #pragma unroll
 for(int k=0;k<4;k++){
  const uint32_t pair=(k<2?lo.x:lo.y)>>(16*(k%2));const uint32_t ed=(de>>(8*k))&255;
  const uint16_t a=(pair&127)|((pair&128)<<8)|((h+(ed&15))<<7);
  const uint16_t b=((pair>>8)&127)|(pair&0x8000)|((h+(ed>>4))<<7);
  words[k]=uint32_t(a)|(uint32_t(b)<<16);
 }
 return make_uint4(words[0],words[1],words[2],words[3]);
}
template <int TILEV, int MAX_T = kFusedGrMaxT, bool Q8 = false, bool PACKED = false>
__global__ void __launch_bounds__(THREADS) gr_down_multi_kernel(GrMulti m) {
    constexpr int TQ = TILEV / 8 / 32;      // uint4 weight chunks per lane per tile
    extern __shared__ __align__(16) float tile[];   // [T][TILEV]
    const int t = threadIdx.x, lane = t & 31, warp = t >> 5;
    const int T = m.T;
    const bool inject_block = blockIdx.x == DOWN_BLOCKS;
    const int row = inject_block ? warp : blockIdx.x * WARPS + warp;
    const bool active = !(inject_block && (m.a[0].w_inject == nullptr || warp >= HC));
    const uint16_t* wmat = inject_block ? m.a[0].w_inject : m.a[0].w_down;
    const float* sq = (Q8 && !inject_block) ? m.a[0].s_down : nullptr;   // the inject rows stay bf16
    const bool q8 = Q8 && !inject_block;
    const size_t row0 = (size_t) (active ? row : 0) * D;
    float acc[MAX_T];
#pragma unroll
    for (int k = 0; k < MAX_T; ++k) acc[k] = 0.0f;
    for (int base = 0; base < D; base += TILEV) {
        uint4 wv[TQ];
        if (active) {
#pragma unroll
            for (int q = 0; q < TQ; ++q) {
                const size_t e = row0 + (size_t)base + (size_t)(lane+32*q)*8;
                if constexpr (PACKED) wv[q] = inject_block ? ldw<Q8>(wmat,sq,e) : lossless8(m.packed,m.fallback,e);
                else wv[q] = ldw<Q8>(wmat,sq,e);
            }
        }
        __syncthreads();                                   // the previous tile is consumed
        const float4* src4 = reinterpret_cast<const float4*>(m.xn);
        float4* tile4 = reinterpret_cast<float4*>(tile);
        for (int i = t; i < T * (TILEV / 4); i += THREADS) {
            const int k = i / (TILEV / 4), off = i - k * (TILEV / 4);
            tile4[i] = src4[((size_t) k * D + base) / 4 + off];
        }
        __syncthreads();
        if (!active) continue;
#pragma unroll
        for (int q = 0; q < TQ; ++q) {
            const int j = lane + 32 * q;
#pragma unroll
            for (int k = 0; k < MAX_T; ++k)
                if (k < T) acc[k] += dotw<Q8>(q8, wv[q], tile + k * TILEV + j * 8);
        }
    }
    if (!active) return;
    float s[MAX_T];
#pragma unroll
    for (int k = 0; k < MAX_T; ++k) s[k] = k < T ? warp_sum(acc[k]) : 0.0f;
    // lane k writes token k (every lane holds every sum after the xor reduction)
#pragma unroll
    for (int k = 0; k < MAX_T; ++k) {
        if (k >= T || lane != k) continue;
        if (inject_block) {
            m.a[k].inject_out[row] = s[k];
        } else {
            const float x = s[k] / (float) HC;
            m.a[k].lo[row] = x / (1.0f + __expf(-x));
        }
    }
}

void launch_common(const float*x,const uint16_t*w,const uint8_t*p,const uint16_t*fb,float*y,int ni,int no,int nt,hipStream_t st,bool fast){
 GrMulti m{};m.xn=const_cast<float*>(x);m.T=nt;m.packed=p;m.fallback=fb;
 for(int k=0;k<nt;k++){m.a[k].w_down=w;m.a[k].lo=y+k*no;}
 if(fast)gr_down_multi_kernel<1280,kFusedGrMaxT,false,true><<<41,256,nt*1280*4,st>>>(m);
 else gr_down_multi_kernel<1280,kFusedGrMaxT,false,false><<<41,256,nt*1280*4,st>>>(m);
}
void launch(const float*x,const uint8_t*p,const uint16_t*fb,float*y,int ni,int no,int nt,hipStream_t st){launch_common(x,nullptr,p,fb,y,ni,no,nt,st,true);}
void raw_launch(const float*x,int64_t,const uint16_t*w,float*y,int64_t,int ni,int no,int nt,hipStream_t st){launch_common(x,w,nullptr,nullptr,y,ni,no,nt,st,false);}
}
int main(int argc,char**argv){
 if(argc!=5)return 99;
 const size_t off=strtoull(argv[2],nullptr,10);const int ni=atoi(argv[3]),no=atoi(argv[4]);
 const size_t n=(size_t)ni*no;std::vector<uint16_t>w(n);
 FILE*f=fopen(argv[1],"rb");if(!f||fseek(f,(long)off,SEEK_SET)||fread(w.data(),2,n,f)!=n)return 98;fclose(f);
 std::vector<uint8_t> packed(n/64*100);std::vector<uint16_t>fb;
 for(size_t b=0;b<n/64;b++){
  int lo=255,hi=0;for(int i=0;i<64;i++){int e=(w[b*64+i]>>7)&255;lo=std::min(lo,e);hi=std::max(hi,e);}
  uint32_t h=lo;uint8_t*p=packed.data()+b*100;
  if(hi-lo>15){h=0x80000000u|(fb.size()/64);fb.insert(fb.end(),w.begin()+b*64,w.begin()+(b+1)*64);}
  else for(int j=0;j<32;j++){
   uint16_t a=w[b*64+2*j],c=w[b*64+2*j+1];
   p[4+2*j]=(a&127)|((a>>8)&128);p[5+2*j]=(c&127)|((c>>8)&128);
   p[68+j]=(((a>>7)&255)-lo)|(((((c>>7)&255)-lo))<<4);
  }
  memcpy(p,&h,4);
  for(int i=0;i<64;i++){
   uint16_t v;
   if(h>>31)v=fb[(h&0x7fffffffu)*64+i];
   else{unsigned l=p[4+i];unsigned e=(p[68+i/2]>>(4*(i%2)))&15;v=(l&127)|((l&128)<<8)|((h+e)<<7);}
   if(v!=w[b*64+i]){fprintf(stderr,"CPU MISMATCH\n");return 3;}
  }
 }
 fprintf(stdout,"ENCODE elements=%zu original=%zu packed=%zu fallback=%zu\n",n,n*2,packed.size(),fb.size()*2);fflush(stdout);
 constexpr int copies=8;std::vector<uint16_t*> dw(copies),df(copies);std::vector<uint8_t*>dp(copies);
 for(int i=0;i<copies;i++){
  OK(hipMalloc(&dw[i],n*2));OK(hipMemcpy(dw[i],w.data(),n*2,hipMemcpyHostToDevice));
  OK(hipMalloc(&dp[i],packed.size()));OK(hipMemcpy(dp[i],packed.data(),packed.size(),hipMemcpyHostToDevice));
  OK(hipMalloc(&df[i],std::max<size_t>(2,fb.size()*2)));if(!fb.empty())OK(hipMemcpy(df[i],fb.data(),fb.size()*2,hipMemcpyHostToDevice));
 }
 float *dx,*dy,*dz;OK(hipMalloc(&dx,ni*8*4));OK(hipMalloc(&dy,no*8*4));OK(hipMalloc(&dz,no*8*4));
 std::vector<float>x(ni*8),a(no*8),b(no*8);for(size_t i=0;i<x.size();i++)x[i]=sinf(float(i)*.037f)*.75f;
 OK(hipMemcpy(dx,x.data(),x.size()*4,hipMemcpyHostToDevice));
 hipStream_t st;OK(hipStreamCreate(&st));hipEvent_t begin,end;OK(hipEventCreate(&begin));OK(hipEventCreate(&end));
 for(int nt=1;nt<=8;nt++){
  strata::kernels::raw_launch(dx,ni,dw[0],dy,no,ni,no,nt,st);
  strata::kernels::launch(dx,dp[0],df[0],dz,ni,no,nt,st);OK(hipStreamSynchronize(st));
  OK(hipMemcpy(a.data(),dy,no*nt*4,hipMemcpyDeviceToHost));OK(hipMemcpy(b.data(),dz,no*nt*4,hipMemcpyDeviceToHost));
  size_t bad=0;for(int i=0;i<no*nt;i++)if(!std::isfinite(a[i])||memcmp(&a[i],&b[i],4))bad++;
  if(bad){fprintf(stdout,"MISMATCH T=%d count=%zu\n",nt,bad);return 4;}
  float times[4];
  for(int arm=0;arm<4;arm++){
   bool fast=arm==1||arm==2;for(int j=0;j<12;j++)if(fast)strata::kernels::launch(dx,dp[j%copies],df[j%copies],dz,ni,no,nt,st);else strata::kernels::raw_launch(dx,ni,dw[j%copies],dy,no,ni,no,nt,st);
   OK(hipEventRecord(begin,st));for(int j=0;j<200;j++)if(fast)strata::kernels::launch(dx,dp[j%copies],df[j%copies],dz,ni,no,nt,st);else strata::kernels::raw_launch(dx,ni,dw[j%copies],dy,no,ni,no,nt,st);
   OK(hipEventRecord(end,st));OK(hipEventSynchronize(end));OK(hipEventElapsedTime(&times[arm],begin,end));
  }
  printf("PARITY_BENCH T=%d base_us=%.3f/%.3f packed_us=%.3f/%.3f\n",nt,times[0]*5,times[3]*5,times[1]*5,times[2]*5);fflush(stdout);
 }
 for(int i=0;i<copies;i++){hipFree(dw[i]);hipFree(dp[i]);hipFree(df[i]);}hipFree(dx);hipFree(dy);hipFree(dz);hipStreamDestroy(st);hipEventDestroy(begin);hipEventDestroy(end);return 0;
}
