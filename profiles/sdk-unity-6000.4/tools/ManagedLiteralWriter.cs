// Bounded Mono/CIL ldstr replacement. No discovery, redirection or new methods.
using System;
using System.IO;
using System.Text;
using System.Linq;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using System.Web.Script.Serialization;
using Mono.Cecil;
using Mono.Cecil.Cil;

public static class ManagedLiteralWriter {
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue };
    static Dictionary<string,object> Dict(object value) { return (Dictionary<string,object>)value; }
    static IEnumerable<object> Rows(object value) { return ((IEnumerable)value).Cast<object>(); }
    static string Text(object value) { return Convert.ToString(value, CultureInfo.InvariantCulture); }
    static int Num(object value) { return Convert.ToInt32(value, CultureInfo.InvariantCulture); }
    static void Require(bool condition, string code) { if (!condition) throw new InvalidDataException(code); }
    static IEnumerable<TypeDefinition> Types(IEnumerable<TypeDefinition> types) {
        foreach (var type in types) {
            yield return type;
            foreach (var child in Types(type.NestedTypes)) yield return child;
        }
    }
    static string Hash(byte[] bytes) {
        using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
    }
    static string Operand(object value, Instruction[] body) {
        if (value == null) return "";
        var branch = value as Instruction;
        if (branch != null) return "branch:" + Array.IndexOf(body, branch);
        var branches = value as Instruction[];
        if (branches != null) return "branches:" + String.Join(",", branches.Select(x => Array.IndexOf(body,x).ToString()).ToArray());
        var member = value as MemberReference;
        if (member != null) return member.FullName;
        var parameter = value as ParameterDefinition;
        if (parameter != null) return "parameter:" + parameter.Index;
        var variable = value as VariableDefinition;
        if (variable != null) return "variable:" + variable.Index;
        return Text(value);
    }
    static string Body(MethodDefinition method) {
        if (!method.HasBody) return "NO_BODY";
        var all = method.Body.Instructions.ToArray();
        Func<Instruction,int> index = x => x == null ? -1 : Array.IndexOf(all,x);
        return Json.Serialize(new {
            method.Body.InitLocals, method.Body.MaxStackSize,
            variables = method.Body.Variables.Select(x => x.VariableType.FullName).ToArray(),
            instructions = all.Select(x => x.OpCode.Code + "|" + Operand(x.Operand,all)).ToArray(),
            handlers = method.Body.ExceptionHandlers.Select(x => new {
                kind = x.HandlerType.ToString(), ts=index(x.TryStart), te=index(x.TryEnd),
                hs=index(x.HandlerStart), he=index(x.HandlerEnd), fs=index(x.FilterStart),
                type=x.CatchType == null ? "" : x.CatchType.FullName }).ToArray()
        });
    }
    static string Metadata(ModuleDefinition module) {
        return Json.Serialize(new {
            module.Name, module.Mvid,
            types = Types(module.Types).Select(x => new {
                x.FullName, token=x.MetadataToken.ToInt32(), attributes=(int)x.Attributes,
                baseType=x.BaseType == null ? "" : x.BaseType.FullName,
                fields=x.Fields.Select(f => f.MetadataToken.ToInt32()+"|"+f.FullName+"|"+(int)f.Attributes).ToArray(),
                methods=x.Methods.Select(m => m.MetadataToken.ToInt32()+"|"+m.FullName+"|"+(int)m.Attributes+"|"+(int)m.ImplAttributes).ToArray()
            }).ToArray(),
            resources=module.Resources.Select(r => new {
                r.Name, attributes=(int)r.Attributes, kind=r.ResourceType.ToString(),
                data=r is EmbeddedResource ? Hash(((EmbeddedResource)r).GetResourceData()) : Json.Serialize(r)
            }).ToArray()
        });
    }
    public static int Main(string[] args) {
        if (args.Length != 1) { Console.Error.WriteLine("MANAGED_REQUEST_REQUIRED"); return 2; }
        try { Apply(Dict(Json.DeserializeObject(File.ReadAllText(args[0], Encoding.UTF8)))); return 0; }
        catch (InvalidDataException e) { Console.Error.WriteLine(e.Message); return 1; }
        catch { Console.Error.WriteLine("MANAGED_APPLICATION_FAILED"); return 1; }
    }
    static void Apply(Dictionary<string,object> request) {
        Require(Num(request["schema_version"]) == 1, "MANAGED_REQUEST_SCHEMA_INVALID");
        string input=Path.GetFullPath(Text(request["input"])), output=Path.GetFullPath(Text(request["output"]));
        Require(!String.Equals(input,output,StringComparison.OrdinalIgnoreCase) && !File.Exists(output), "MANAGED_OUTPUT_NOT_NEW");
        Require(Hash(File.ReadAllBytes(input)) == Text(request["sha256"]), "MANAGED_SOURCE_CHANGED");
        var resolver=new DefaultAssemblyResolver(); resolver.AddSearchDirectory(Path.GetDirectoryName(input));
        using (var assembly=AssemblyDefinition.ReadAssembly(input,new ReaderParameters {
            AssemblyResolver=resolver, ReadSymbols=false, InMemory=true })) {
            var module=assembly.MainModule;
            Require(assembly.Modules.Count == 1 && !assembly.Name.HasPublicKey, "MANAGED_ASSEMBLY_LAYOUT_UNSUPPORTED");
            var methods=Types(module.Types).SelectMany(x => x.Methods).ToDictionary(x => x.MetadataToken.ToInt32());
            var expected=methods.ToDictionary(x => x.Key, x => Body(x.Value));
            string originalMetadata=Metadata(module);
            var protectedMethods=new HashSet<int>(Rows(request["protected_methods"]).Select(Num));
            Require(protectedMethods.All(x => methods.ContainsKey(x)), "MANAGED_PROTECTED_BINDING_MISSING");
            var seen=new HashSet<string>();
            int count=0;
            foreach (var raw in Rows(request["literals"])) {
                var row=Dict(raw); int token=Num(row["method_token"]), offset=Num(row["offset"]);
                Require(methods.ContainsKey(token) && !protectedMethods.Contains(token), "MANAGED_METHOD_BINDING_INVALID");
                var method=methods[token];
                Require(method.FullName == Text(row["method_full_name"]) && method.HasBody, "MANAGED_METHOD_CHANGED");
                Require(seen.Add(token+":"+offset), "MANAGED_SITE_AMBIGUOUS");
                var instructions=method.Body.Instructions.Where(x => x.Offset == offset).ToArray();
                Require(instructions.Length == 1 && instructions[0].OpCode.Code == Code.Ldstr
                    && (string)instructions[0].Operand == Text(row["source"]), "MANAGED_LITERAL_CHANGED");
                Require(row["source"] is string && row["target"] is string, "MANAGED_LITERAL_INVALID");
                instructions[0].Operand=Text(row["target"]); count++;
                expected[token]=Body(method);
            }
            Require(count > 0, "MANAGED_BINDINGS_EMPTY");
            if (request.ContainsKey("operation") && Text(request["operation"]) == "inspect") {
                Console.WriteLine(Json.Serialize(new {status="bound", literals=count, runtime_verified=false}));
                return;
            }
            Require(!request.ContainsKey("operation") || Text(request["operation"]) == "apply", "MANAGED_OPERATION_INVALID");
            assembly.Write(output,new WriterParameters { WriteSymbols=false });
            using (var reread=AssemblyDefinition.ReadAssembly(output,new ReaderParameters {
                AssemblyResolver=resolver, ReadSymbols=false, InMemory=true })) {
                var after=Types(reread.MainModule.Types).SelectMany(x => x.Methods).ToDictionary(x => x.MetadataToken.ToInt32());
                Require(after.Count == methods.Count && expected.All(x => after.ContainsKey(x.Key) && Body(after[x.Key]) == x.Value),
                    "MANAGED_BODY_READBACK_CHANGED");
                Require(Metadata(reread.MainModule) == originalMetadata, "MANAGED_METADATA_READBACK_CHANGED");
            }
            Console.WriteLine(Json.Serialize(new {status="verified", literals=count, runtime_verified=false}));
        }
    }
}
