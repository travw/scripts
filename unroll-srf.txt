using System;
using System.Collections;
using System.Collections.Generic;

using Rhino;
using Rhino.Geometry;

using Grasshopper;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Data;
using Grasshopper.Kernel.Types;
using System.IO;
using System.Linq;
using System.Data;
using System.Drawing;
using System.Reflection;
using System.Windows.Forms;
using System.Xml;
using System.Xml.Linq;
using System.Runtime.InteropServices;

using Rhino.DocObjects;
using Rhino.Collections;
using GH_IO;
using GH_IO.Serialization;

public class Script_Instance : GH_ScriptInstance
{
  private void Print(string text) { /* Implementation hidden. */ }
  private void Print(string format, params object[] args) { /* Implementation hidden. */ }
  private void Reflect(object obj) { /* Implementation hidden. */ }
  private void Reflect(object obj, string method_name) { /* Implementation hidden. */ }

  private readonly RhinoDoc RhinoDocument;
  private readonly GH_Document GrasshopperDocument;
  private readonly IGH_Component Component;
  private readonly int Iteration;

  /// <summary>
  /// Input: Brep brep, List<Curve> curves
  /// Output: A = unrolled breps, B = unrolled curves
  /// </summary>
  private void RunScript(
		Brep brep,
		List<Curve> curves,
		ref object A,
		ref object B)
  {
    Unroller unroll = new Unroller(brep);

    // Add curves to follow the unroll
    if (curves != null)
    {
      foreach (Curve crv in curves)
      {
        if (crv != null)
          unroll.AddFollowingGeometry(crv);
      }
    }

    Curve[] outCrvs;
    Point3d[] pts;
    TextDot[] tDots;

    Brep[] breps = unroll.PerformUnroll(out outCrvs, out pts, out tDots);

    A = breps;
    B = outCrvs;
  }

  // <Custom additional code> 

  // </Custom additional code> 
}