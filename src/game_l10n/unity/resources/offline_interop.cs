using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.Loader;
using System.Text.RegularExpressions;
using AssetRipper.Primitives;
using Cpp2IL.Core;
using Cpp2IL.Core.Api;
using Cpp2IL.Core.InstructionSets;
using Cpp2IL.Core.OutputFormats;
using Cpp2IL.Core.ProcessingLayers;
using Il2CppInterop.Generator;
using Il2CppInterop.Common;
using Il2CppInterop.Generator.Runners;
using LibCpp2IL;
using Microsoft.Extensions.Logging.Abstractions;

public static class InteropRunner
{
    public static int Main(string[] args)
    {
        if (args.Length != 8)
        {
            Console.Error.WriteLine("Expected loader, input, output, and Unity version arguments.");
            return 2;
        }
        var coreRoot = Path.GetFullPath(args[0]);
        var runtimeRoot = Path.GetFullPath(args[1]);
        AssemblyLoadContext.Default.Resolving += (_, name) => ResolveAssembly(name, coreRoot, runtimeRoot);
        try
        {
            return Generate(args);
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(exception);
            return 2;
        }
    }

    private static Assembly? ResolveAssembly(AssemblyName name, params string[] roots)
    {
        foreach (var root in roots)
        {
            var candidate = Path.Combine(root, name.Name + ".dll");
            if (File.Exists(candidate))
                return AssemblyLoadContext.Default.LoadFromAssemblyPath(candidate);
        }
        return null;
    }

    private static UnityVersion ParseUnityVersion(string value)
    {
        var match = Regex.Match(value, @"^(\d+)\.(\d+)\.(\d+)");
        if (!match.Success)
            throw new ArgumentException("Unity version must begin with major.minor.patch.");
        return new UnityVersion(
            ushort.Parse(match.Groups[1].Value),
            ushort.Parse(match.Groups[2].Value),
            ushort.Parse(match.Groups[3].Value));
    }

    private static int Generate(string[] args)
    {
        InstructionSetRegistry.RegisterInstructionSet<X86InstructionSet>(DefaultInstructionSets.X86_32);
        InstructionSetRegistry.RegisterInstructionSet<X86InstructionSet>(DefaultInstructionSets.X86_64);
        LibCpp2IlBinaryRegistry.RegisterBuiltInBinarySupport();
        Cpp2IlApi.InitializeLibCpp2Il(args[2], args[3], ParseUnityVersion(args[6]), false);
        Console.WriteLine("{\"phase\":\"metadata_parse\",\"status\":\"passed\",\"metadata_version\":" + args[7] + "}");

        var layers = new List<Cpp2IlProcessingLayer> { new AttributeInjectorProcessingLayer() };
        foreach (var layer in layers)
            layer.PreProcess(Cpp2IlApi.CurrentAppContext, layers);
        foreach (var layer in layers)
            layer.Process(Cpp2IlApi.CurrentAppContext);
        var assemblies = new AsmResolverDllOutputFormatDefault().BuildAssemblies(Cpp2IlApi.CurrentAppContext);
        Console.WriteLine("{\"phase\":\"dummy_assemblies\",\"status\":\"passed\",\"assemblies\":" + assemblies.Count + "}");

        LibCpp2IlMain.Reset();
        Cpp2IlApi.CurrentAppContext = null;
        var options = new GeneratorOptions
        {
            GameAssemblyPath = args[2],
            Source = assemblies,
            OutputDir = args[5],
            UnityBaseLibsDir = args[4]
        };
        Il2CppInteropGenerator.Create(options)
            .AddLogger(NullLogger.Instance)
            .AddInteropAssemblyGenerator()
            .Run();

        var generated = Directory.GetFiles(args[5], "*.dll");
        if (generated.Length == 0)
            throw new InvalidOperationException("Interop generation produced no assemblies.");
        foreach (var path in generated)
            AssemblyName.GetAssemblyName(path);
        Console.WriteLine("{\"phase\":\"interop_generation\",\"status\":\"passed\",\"assemblies\":" + generated.Length + "}");
        return 0;
    }
}
