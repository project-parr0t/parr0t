// Builds a synthetic assembly without compiling or executing any player code.
using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Web.Script.Serialization;
using Mono.Cecil;
using Mono.Cecil.Cil;

public static class ManagedFixture {
    public static void Main(string[] args) {
        var assembly=AssemblyDefinition.CreateAssembly(new AssemblyNameDefinition("SyntheticFixture",new Version(1,0)),
            "SyntheticFixture",ModuleKind.Dll);
        var module=assembly.MainModule;
        var type=new TypeDefinition("SyntheticFixture","Widget",TypeAttributes.Public,module.TypeSystem.Object);
        module.Types.Add(type);
        type.Fields.Add(new FieldDefinition("ExistingValue",FieldAttributes.Public,module.TypeSystem.Int32));
        var display=new MethodDefinition("Display",MethodAttributes.Public|MethodAttributes.Static,module.TypeSystem.String);
        display.Body.Instructions.Add(Instruction.Create(OpCodes.Ldstr,"SYNTHETIC_SOURCE_{0}"));
        display.Body.Instructions.Add(Instruction.Create(OpCodes.Ret));
        type.Methods.Add(display);
        var resource=new MethodDefinition("ProtectedResource",MethodAttributes.Public|MethodAttributes.Static,module.TypeSystem.String);
        resource.Body.Instructions.Add(Instruction.Create(OpCodes.Ldstr,"SYNTHETIC_RESOURCE_KEY"));
        resource.Body.Instructions.Add(Instruction.Create(OpCodes.Ret));
        type.Methods.Add(resource);
        var choose=new MethodDefinition("UnrelatedSwitch",MethodAttributes.Public|MethodAttributes.Static,module.TypeSystem.String);
        choose.Parameters.Add(new ParameterDefinition(module.TypeSystem.Int32));
        var first=Instruction.Create(OpCodes.Ldstr,"SYNTHETIC_CASE_A");
        var second=Instruction.Create(OpCodes.Ldstr,"SYNTHETIC_CASE_B");
        var end=Instruction.Create(OpCodes.Ret);
        foreach(var ins in new[] {Instruction.Create(OpCodes.Ldarg_0),Instruction.Create(OpCodes.Switch,new[]{first,second}),
            Instruction.Create(OpCodes.Ldstr,"SYNTHETIC_DEFAULT"),Instruction.Create(OpCodes.Br,end),
            first,Instruction.Create(OpCodes.Br,end),second,end}) choose.Body.Instructions.Add(ins);
        type.Methods.Add(choose);
        module.Resources.Add(new EmbeddedResource("SYNTHETIC_RESOURCE",ManifestResourceAttributes.Private,
            Encoding.UTF8.GetBytes("SYNTHETIC_OPAQUE_RESOURCE_BYTES")));
        assembly.Write(args[0]);
        using(var reread=AssemblyDefinition.ReadAssembly(args[0])) {
            var methods=reread.MainModule.Types.Single(x=>x.Name=="Widget").Methods;
            var method=methods.Single(x=>x.Name=="Display");
            var result=new {method_token=method.MetadataToken.ToInt32(),method_full_name=method.FullName,
                offset=method.Body.Instructions[0].Offset,source="SYNTHETIC_SOURCE_{0}",
                protected_method=methods.Single(x=>x.Name=="ProtectedResource").MetadataToken.ToInt32()};
            File.WriteAllText(args[1],new JavaScriptSerializer().Serialize(result));
        }
        assembly.Dispose();
    }
}
