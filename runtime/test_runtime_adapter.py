"""Isolated bridge ABI/runtime fixtures; never attaches to WeChat or sends messages."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent

HARNESS = r'''
#include "native_message_bridge.cpp"
#include <cassert>
#include <iostream>
struct Fixture {
 uint64_t padding[2]{};
 std::string to;
 std::string text;
 uint32_t type=0;
 uint32_t reserved=0;
 uint64_t length=0;
 std::string image;
 std::string payload;
 uint32_t duration=0;
};
static InputLayout layout(){
 return {offsetof(Fixture,to),offsetof(Fixture,text),offsetof(Fixture,type),offsetof(Fixture,length),offsetof(Fixture,image),offsetof(Fixture,payload),offsetof(Fixture,duration),-16,24,24,32,16,24};
}
static Shared text_factory(){return std::make_shared<Fixture>();}
static Shared voice_factory(void*,const std::string& to,const std::string& payload,const uint32_t* duration,const VoiceOptions&){
 auto p=std::make_shared<Fixture>();p->to=to;p->payload=payload;p->duration=*duration;return p;
}
static std::function<void()> saved;
static int root_object=0,result_destroyed=0;
static BatchIdentity native_identity;
static void* service_getter(){return &root_object;}
static Shared executor_getter(void*){return std::make_shared<int>(1);}
static Shared context_factory(){return std::make_shared<int>(2);}
static Shared context_mid(void*){return std::make_shared<int>(3);}
static Shared manager_from_mid(void*){return std::make_shared<int>(4);}
static Result submit(void*,const std::vector<Shared>& inputs,bool){
 assert(inputs.size()==1);Result result{};uint64_t flags=9;
 std::memcpy(result.bytes,&flags,8);
 auto begin=reinterpret_cast<uintptr_t>(&native_identity),end=begin+sizeof(BatchIdentity);
 std::memcpy(result.bytes+16,&begin,8);std::memcpy(result.bytes+24,&end,8);return result;
}
static void destroy_result(void*){++result_destroyed;}
static Shared schedule(void*,const Location*,std::function<void()>* work,bool){saved=*work;return std::make_shared<int>(5);}
int main(){
 assert(rea_bridge_abi_version()==2);auto l=layout();
 auto text=rea_make_text_input(reinterpret_cast<void*>(text_factory),"filehelper","hello",&l);
 auto* input=static_cast<Fixture*>(rea_text_input_object(text));
 assert(input->to=="filehelper"&&input->text=="hello"&&input->type==1&&input->length==5);
 native_identity.first="recipient";native_identity.second="record";native_identity.words[0]=12;native_identity.words[1]=34;
 ScheduleFunctions f{reinterpret_cast<void*>(service_getter),reinterpret_cast<void*>(executor_getter),reinterpret_cast<void*>(context_factory),reinterpret_cast<void*>(context_mid),reinterpret_cast<void*>(manager_from_mid),reinterpret_cast<void*>(submit),reinterpret_cast<void*>(destroy_result),reinterpret_cast<void*>(schedule)};
 ResultLayout r{16,24};rea_schedule_text(&f,text,&r);
 assert(rea_work_state(text)==0&&saved);
 // Prove native async work owns values, not pointers into Frida temporary data.
 std::memset(&f,0,sizeof(f));r={0,0};saved();saved=nullptr;
 assert(rea_work_state(text)==2&&rea_work_flags(text)==9&&result_destroyed==1);
 assert(rea_work_identity_count(text)==1);
 assert(std::string(rea_work_identity_string(text,0))=="recipient");
 assert(std::string(rea_work_identity_string(text,1))=="record");
 assert(rea_work_identity_word(text,0)==12&&rea_work_identity_word(text,1)==34);
 rea_destroy_text_input(text);
 auto image=rea_make_image_input(reinterpret_cast<void*>(text_factory),"recipient","/tmp/fixture.png",&l);
 input=static_cast<Fixture*>(rea_text_input_object(image));assert(input->to=="recipient"&&input->image=="/tmp/fixture.png"&&input->type==3);rea_destroy_text_input(image);
 const char bytes[]={'a','\0','b'};
 auto voice=rea_make_voice_input(reinterpret_cast<void*>(voice_factory),"recipient",bytes,3,1234,&l);
 input=static_cast<Fixture*>(rea_text_input_object(voice));assert(input->to=="recipient"&&input->payload==std::string(bytes,3)&&input->duration==1234&&input->type==34);rea_destroy_text_input(voice);
 auto invalid=l;invalid.text=-8;assert(!rea_make_text_input(reinterpret_cast<void*>(text_factory),"recipient","test",&invalid));
 assert(!rea_make_text_input(nullptr,"recipient","test",&l));
 std::cout<<"bridge ABI v2: fields, native lifetime, async config copies, result identity passed\n";
}
'''


class RuntimeAdapterTests(unittest.TestCase):
    def test_runtime_fixtures(self):
        node = shutil.which('node')
        self.assertIsNotNone(node)
        result = subprocess.run([node, '--test', str(ROOT / 'test_runtime_adapter.js')], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('pass 19', result.stdout)

    @unittest.skipUnless(shutil.which('clang++'), 'clang++ unavailable')
    def test_bridge_abi_and_async_config_ownership(self):
        with tempfile.TemporaryDirectory(prefix='rea-bridge-abi-') as name:
            path = Path(name)
            source = path / 'harness.cpp'
            source.write_text(HARNESS)
            built = subprocess.run(['clang++', '-std=c++17', '-I', str(ROOT), str(source), '-o', str(path / 'harness')], capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(path / 'harness')], capture_output=True, text=True, timeout=10)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn('async config copies', ran.stdout)


if __name__ == '__main__':
    unittest.main()
