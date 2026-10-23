#include <memory>
#include <vector>
#include <string>
#include <functional>
#include <atomic>
#include <cstdint>
#include <cstring>
#include <cstddef>
using Shared=std::shared_ptr<void>;
struct BatchIdentity {std::string first; uint64_t words[2]{}; std::string second;};
static_assert(sizeof(BatchIdentity)==64 && offsetof(BatchIdentity,words)==0x18 && offsetof(BatchIdentity,second)==0x28);
struct InputBox {
 std::vector<Shared> inputs; std::atomic<int> state{0}; uint64_t result_flags=0;
 uint64_t identity_count=0; BatchIdentity identity;
};
struct Location {const char* function; const char* file; int line; void* pc;};
// Non-trivial return forces the Darwin arm64 indirect-result ABI (x8).
struct Result {alignas(8) unsigned char bytes[64]; ~Result() {}};
static_assert(sizeof(Shared)==16 && sizeof(std::string)==24 && sizeof(Location)==32 && sizeof(Result)==64);
// ABI v2: 13 int32 entries, same order as runtime.js LAYOUT_FIELDS. No client
// offsets live in the bridge. Copy all caller-owned config before native work.
struct InputLayout {
 int32_t recipient,text,type,text_length,image_path,voice_payload,voice_duration;
 int32_t manager_adjust,manager_ready,service_manager_slot,service_login_slot;
 int32_t result_begin,result_end;
};
static_assert(sizeof(InputLayout)==52);
struct ScheduleFunctions {
 void* service_getter; void* executor_getter; void* context_factory; void* context_mid;
 void* manager_from_mid; void* message_submit; void* result_destroy; void* executor_schedule;
};
static_assert(sizeof(ScheduleFunctions)==64);
struct ResultLayout {uint32_t begin,end;};
static bool field(int32_t value,uint32_t width,uint32_t alignment){
 return value>=8 && static_cast<uint32_t>(value)<=65536-width && value%alignment==0;
}
static bool valid(const InputLayout& l){
 return field(l.recipient,24,8)&&field(l.text,24,8)&&field(l.type,4,4)&&field(l.text_length,8,8)
  &&field(l.image_path,24,8)&&field(l.voice_payload,24,8)&&field(l.voice_duration,4,4);
}
extern "C" __attribute__((visibility("default"))) uint32_t rea_bridge_abi_version(){return 2;}
extern "C" __attribute__((visibility("default")))
void* rea_make_text_input(void* factory,const char*recipient,const char*text,const InputLayout* layout){
 if(!factory||!recipient||!text||!layout)return nullptr;
 const auto l=*layout;if(!valid(l))return nullptr;
 auto input=reinterpret_cast<Shared(*)()>(factory)();if(!input)return nullptr;
 auto*p=static_cast<unsigned char*>(input.get());
 *reinterpret_cast<std::string*>(p+l.recipient)=recipient;
 *reinterpret_cast<std::string*>(p+l.text)=text;
 *reinterpret_cast<uint32_t*>(p+l.type)=1;
 *reinterpret_cast<uint64_t*>(p+l.text_length)=std::strlen(text);
 auto*box=new InputBox;box->inputs.push_back(std::move(input));return box;
}
extern "C" __attribute__((visibility("default")))
void* rea_make_image_input(void* factory,const char*recipient,const char*path,const InputLayout* layout){
 if(!factory||!recipient||!path||!layout)return nullptr;
 const auto l=*layout;if(!valid(l))return nullptr;
 auto input=reinterpret_cast<Shared(*)()>(factory)();if(!input)return nullptr;
 auto*p=static_cast<unsigned char*>(input.get());
 *reinterpret_cast<std::string*>(p+l.recipient)=recipient;
 *reinterpret_cast<uint32_t*>(p+l.type)=3;
 *reinterpret_cast<std::string*>(p+l.image_path)=path;
 auto*box=new InputBox;box->inputs.push_back(std::move(input));return box;
}
extern "C" __attribute__((visibility("default"))) void* rea_text_input_object(void*p){return static_cast<InputBox*>(p)->inputs.at(0).get();}
extern "C" __attribute__((visibility("default"))) void rea_destroy_text_input(void*p){delete static_cast<InputBox*>(p);}
extern "C" __attribute__((visibility("default"))) int rea_work_state(void*p){return static_cast<InputBox*>(p)->state.load();}
extern "C" __attribute__((visibility("default"))) uint64_t rea_work_flags(void*p){return static_cast<InputBox*>(p)->result_flags;}
// Read only after state == 2; returned strings remain owned by InputBox until destroy.
extern "C" __attribute__((visibility("default"))) uint64_t rea_work_identity_count(void*p){
 auto*box=static_cast<InputBox*>(p);return box->state.load()==2?box->identity_count:0;
}
extern "C" __attribute__((visibility("default"))) const char* rea_work_identity_string(void*p,uint32_t index){
 auto*box=static_cast<InputBox*>(p);
 if(box->state.load()!=2 || box->identity_count!=1 || index>1)return nullptr;
 return (index==0?box->identity.first:box->identity.second).c_str();
}
extern "C" __attribute__((visibility("default"))) uint64_t rea_work_identity_word(void*p,uint32_t index){
 auto*box=static_cast<InputBox*>(p);
 if(box->state.load()!=2 || box->identity_count!=1 || index>1)return 0;
 return box->identity.words[index];
}
extern "C" __attribute__((visibility("default"))) void rea_schedule_text(const ScheduleFunctions* functions,void*p,const ResultLayout* result_layout){
 if(!functions||!p||!result_layout)return;
 const auto f=*functions;const auto r=*result_layout;auto*box=static_cast<InputBox*>(p);
 if(!f.service_getter||!f.executor_getter||!f.context_factory||!f.context_mid||!f.manager_from_mid
  ||!f.message_submit||!f.result_destroy||!f.executor_schedule||r.begin>56||r.end>56||r.begin%8||r.end%8||r.begin==r.end){
  box->state.store(-1);return;
 }
 auto root=reinterpret_cast<void*(*)()>(f.service_getter)();
 auto executor=reinterpret_cast<Shared(*)(void*)>(f.executor_getter)(root);
 // f and r are values. The caller's temporary Frida buffers may disappear as
 // soon as this function returns; the actual std::function ABI is preserved.
 std::function<void()> work=[f,r,box]{
  box->state.store(1);
  auto context=reinterpret_cast<Shared(*)()>(f.context_factory)();
  auto mid=reinterpret_cast<Shared(*)(void*)>(f.context_mid)(context.get());
  auto manager=reinterpret_cast<Shared(*)(void*)>(f.manager_from_mid)(mid.get());
  auto result=reinterpret_cast<Result(*)(void*,const std::vector<Shared>&,bool)>(f.message_submit)(manager.get(),box->inputs,true);
  std::memcpy(&box->result_flags,result.bytes,8);
  uintptr_t first=0,last=0;
  std::memcpy(&first,result.bytes+r.begin,sizeof(first));
  std::memcpy(&last,result.bytes+r.end,sizeof(last));
  if(last>=first && (last-first)%sizeof(BatchIdentity)==0){
   box->identity_count=(last-first)/sizeof(BatchIdentity);
   if(first && box->identity_count==1)box->identity=*reinterpret_cast<const BatchIdentity*>(first);
  }
  reinterpret_cast<void(*)(void*)>(f.result_destroy)(&result);
  box->state.store(2);
 };
 Location loc{"rea_schedule_text","rea_local_bridge.cpp",1,nullptr};
 auto scheduled=reinterpret_cast<Shared(*)(void*,const Location*,std::function<void()>*,bool)>(f.executor_schedule)(executor.get(),&loc,&work,true);
}

struct VoiceOptions {uint32_t values[4]={4,3,30000,0};uint64_t flags=0;std::string extra;};
static_assert(sizeof(VoiceOptions)==48);
extern "C" __attribute__((visibility("default")))
void* rea_make_voice_input(void* factory,const char*recipient,const void*data,uint64_t length,uint32_t duration_ms,const InputLayout* layout){
 if(!factory||!recipient||!data||!layout)return nullptr;
 const auto l=*layout;if(!valid(l))return nullptr;
 std::string to(recipient),payload(static_cast<const char*>(data),length);VoiceOptions options;
 auto input=reinterpret_cast<Shared(*)(void*,const std::string&,const std::string&,const uint32_t*,const VoiceOptions&)>(factory)(nullptr,to,payload,&duration_ms,options);
 if(!input)return nullptr;
 *reinterpret_cast<uint32_t*>(static_cast<unsigned char*>(input.get())+l.type)=34;
 auto*box=new InputBox;box->inputs.push_back(std::move(input));return box;
}
