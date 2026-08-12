// ============================================================================
//  mesh_and_remesh — labeled image -> manifold multi-material tet mesh (CGAL)
// ----------------------------------------------------------------------------
//  Why this and not "remesh the existing .mesh"?
//  --------------------------------------------------------------------------
//  CGAL's tetrahedral remeshing needs a valid CGAL::Triangulation_3, which
//  cannot represent a tet mesh whose *outer boundary is non-manifold*. The
//  pipeline's pygalmesh output is exactly that: measured 186 non-manifold
//  boundary edges (surface pinch points) on the mouse mesh, so read_MEDIT ->
//  build a Triangulation_3 fails (assign_neighbors: a facet with != 2 incident
//  cells). You cannot remesh what you cannot load.
//
//  The robust fix is to mesh AND remesh inside CGAL, from the labeled image,
//  asking Mesh_3 for a MANIFOLD surface. Then:
//    - the surface is clean (the pinch-point spikes are resolved at meshing
//      time, not patched afterwards),
//    - Mesh_3's optimizers (perturb/exude) already give well-shaped tets,
//    - tetrahedral_isotropic_remeshing then makes the elements isotropic and
//      tangentially smooths the boundary (kills the remaining oversized
//      "pimple" triangles) while preserving every subdomain interface,
//    - the result is a valid, manifold, conformal multi-material mesh that is
//      itself reloadable/remeshable, and MMC-ready.
//
//  Subdomain refs = the image's label values directly (Labeled_mesh_domain_3),
//  so the Medit output already carries the true tissue labels (1,2,4,5,6,7…),
//  no CGAL sequential renumbering to undo downstream.
//
//  Input:  an uncompressed INRIMAGE (.inr) written by npy2inr.py
//          (pygalmesh.save_inr) from 02_merged.npy + INFO.txt spacing.
//  Output: a Medit .mesh (Vertices / Triangles / Tetrahedra with refs).
//
//  Usage:
//    mesh_and_remesh <in.inr> <out.mesh>
//        [--facet-angle A]      (deg, default 30)
//        [--facet-size S]       (mm,  default 1.0)     surface triangle size
//        [--facet-distance D]   (mm,  default 0.1)     surface approx. error
//        [--cell-size C]        (mm,  default 2.0)     tet size
//        [--cell-ratio R]       (default 3.0)          radius-edge ratio
//        [--no-manifold]        (allow non-manifold surface)
//        [--no-remesh]          (skip the isotropic remeshing pass)
//        [--target-edge-length L] (mm, default = facet-size) remeshing target
//        [--iterations N]       (default 3)            remeshing sweeps
//
//  Build: see CMakeLists.txt / build.ps1 in this folder.
// ============================================================================

#include <CGAL/Exact_predicates_inexact_constructions_kernel.h>
#include <CGAL/Mesh_triangulation_3.h>
#include <CGAL/Mesh_complex_3_in_triangulation_3.h>
#include <CGAL/Mesh_criteria_3.h>
#include <CGAL/Labeled_mesh_domain_3.h>
#include <CGAL/make_mesh_3.h>
#include <CGAL/Image_3.h>
#include <CGAL/tetrahedral_remeshing.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <map>
#include <string>

typedef CGAL::Exact_predicates_inexact_constructions_kernel K;
typedef CGAL::Labeled_mesh_domain_3<K> Mesh_domain;
// Parallel Mesh_3 when built against Intel TBB (define CGAL_LINKED_WITH_TBB and
// link -ltbb); otherwise single-threaded. Note: only the Mesh_3 refinement is
// parallelised — tetrahedral remeshing stays sequential, but we run --no-remesh.
#ifdef CGAL_LINKED_WITH_TBB
typedef CGAL::Parallel_tag Concurrency_tag;
#else
typedef CGAL::Sequential_tag Concurrency_tag;
#endif
typedef CGAL::Mesh_triangulation_3<Mesh_domain, CGAL::Default, Concurrency_tag>::type Tr;
typedef CGAL::Mesh_complex_3_in_triangulation_3<Tr> C3t3;
typedef CGAL::Mesh_criteria_3<Tr> Mesh_criteria;

namespace params = CGAL::parameters;

namespace {

struct Options {
    std::string in_path, out_path;
    double facet_angle = 30.0;
    double facet_size = 1.0;
    double facet_distance = 0.1;
    double cell_size = 2.0;
    double cell_ratio = 3.0;
    // manifold() off by default: in CGAL 5.5.2 it loops/OOMs on labeled-image
    // domains. Opt in with --manifold (needs CGAL >= 5.6 to be reliable).
    bool manifold = false;
    bool remesh = true;
    bool remesh_boundaries = true;
    double target_edge = 0.0; // 0 => facet_size
    int iterations = 3;
    double max_tets = 12.0e6; // pre-flight cap (raise if you have the RAM)
};

[[noreturn]] void usage(const char* prog, int code) {
    std::cerr
        << "Usage: " << prog
        << " <in.inr> <out.mesh> [options]\n"
           "  --facet-angle A        min facet angle, deg (default 30)\n"
           "  --facet-size S         surface triangle size, mm (default 1.0)\n"
           "  --facet-distance D     surface approximation error, mm (default 0.1)\n"
           "  --cell-size C          tetrahedron size, mm (default 2.0)\n"
           "  --cell-ratio R         radius-edge ratio (default 3.0)\n"
           "  --manifold             request a manifold surface (CGAL>=5.6; loops in 5.5.2)\n"
           "  --no-remesh            skip the isotropic remeshing pass\n"
           "  --target-edge-length L remeshing target, mm (default = facet-size)\n"
           "  --iterations N         remeshing sweeps (default 3)\n"
           "  --max-tets N           pre-flight tet-count cap; abort if exceeded (default 12e6)\n";
    std::exit(code);
}

Options parse(int argc, char** argv) {
    if (argc < 3) usage(argv[0], 1);
    Options o;
    o.in_path = argv[1];
    o.out_path = argv[2];
    for (int i = 3; i < argc; ++i) {
        std::string a = argv[i];
        auto val = [&](const char* n) -> const char* {
            if (i + 1 >= argc) {
                std::cerr << "Missing value for " << n << "\n";
                usage(argv[0], 1);
            }
            return argv[++i];
        };
        if (a == "--facet-angle")
            o.facet_angle = std::atof(val("--facet-angle"));
        else if (a == "--facet-size")
            o.facet_size = std::atof(val("--facet-size"));
        else if (a == "--facet-distance")
            o.facet_distance = std::atof(val("--facet-distance"));
        else if (a == "--cell-size")
            o.cell_size = std::atof(val("--cell-size"));
        else if (a == "--cell-ratio")
            o.cell_ratio = std::atof(val("--cell-ratio"));
        else if (a == "--manifold")
            o.manifold = true;
        else if (a == "--no-manifold")
            o.manifold = false; // default; kept for clarity
        else if (a == "--no-remesh")
            o.remesh = false;
        else if (a == "--freeze-boundaries")
            o.remesh_boundaries = false;
        else if (a == "--target-edge-length")
            o.target_edge = std::atof(val("--target-edge-length"));
        else if (a == "--iterations")
            o.iterations = std::atoi(val("--iterations"));
        else if (a == "--max-tets")
            o.max_tets = std::atof(val("--max-tets"));
        else if (a == "-h" || a == "--help")
            usage(argv[0], 0);
        else {
            std::cerr << "Unknown argument: " << a << "\n";
            usage(argv[0], 1);
        }
    }
    if (o.target_edge <= 0.0) o.target_edge = o.facet_size;
    return o;
}

std::map<int, std::size_t> subdomain_histogram(const C3t3& c3t3) {
    std::map<int, std::size_t> h;
    for (auto cit = c3t3.cells_in_complex_begin(); cit != c3t3.cells_in_complex_end(); ++cit)
        ++h[static_cast<int>(c3t3.subdomain_index(cit))];
    return h;
}

void print_hist(const char* tag, const std::map<int, std::size_t>& h) {
    std::cout << "  " << tag << " subdomains: ";
    for (auto& kv : h)
        std::cout << kv.first << "=" << kv.second << "  ";
    std::cout << "\n";
}

} // namespace

int main(int argc, char** argv) {
    const Options o = parse(argc, argv);

    CGAL::Image_3 image;
    if (!image.read(o.in_path.c_str())) {
        std::cerr << "ERROR: cannot read INR image: " << o.in_path << "\n";
        return 2;
    }
    std::cout << "=== mesh_and_remesh ===\n";
    std::cout << "  image: " << image.xdim() << " x " << image.ydim() << " x " << image.zdim()
              << "  voxel " << image.vx() << " x " << image.vy() << " x " << image.vz() << " mm\n";
    std::cout << "  facet_angle=" << o.facet_angle << " facet_size=" << o.facet_size
              << " facet_distance=" << o.facet_distance << "\n";
    std::cout << "  cell_size=" << o.cell_size << " cell_ratio=" << o.cell_ratio
              << " manifold=" << (o.manifold ? "yes" : "no") << "\n";

    // ── Pre-flight memory guard ─────────────────────────────────────────────
    // Sub-voxel / voxel-scale edge lengths explode the tet count (n ~ V/edge^3)
    // and OOM (std::bad_alloc) after many minutes. Estimate up front and refuse
    // immediately with the safe minimum, instead of crashing 20 min in.
    const double vmin = std::min({image.vx(), image.vy(), image.vz()});
    double tissue_mm3 = 0.0;
    {
        const double vvox = image.vx() * image.vy() * image.vz();
        std::size_t nb = 0;
        for (std::size_t k = 0; k < image.zdim(); ++k)
            for (std::size_t j = 0; j < image.ydim(); ++j)
                for (std::size_t i = 0; i < image.xdim(); ++i)
                    if (image.value(i, j, k) != 0.f) ++nb;
        tissue_mm3 = nb * vvox;
    }
    const double edge = o.remesh ? o.target_edge : o.cell_size;
    const double est_tets = 8.49 * tissue_mm3 / (edge * edge * edge); // ~regular tets filling V
    const double TET_CAP = o.max_tets; // ~12M fits comfortably in ~24 GB; raise with --max-tets
    std::cout << "  tissue ~" << tissue_mm3 << " mm^3; estimated ~" << static_cast<long>(est_tets)
              << " tets at edge " << edge << " mm (voxel " << vmin << " mm)\n";
    if (edge < vmin) {
        std::cerr << "WARNING: edge length " << edge << " mm is below the voxel size " << vmin
                  << " mm — no sub-voxel detail exists there, only cost.\n";
    }
    if (est_tets > TET_CAP) {
        const double safe = std::cbrt(8.49 * tissue_mm3 / TET_CAP);
        std::cerr << "ERROR: ~" << static_cast<long>(est_tets / 1e6)
                  << "M tetrahedra would exhaust memory (cap ~" << static_cast<long>(TET_CAP / 1e6)
                  << "M). Increase " << (o.remesh ? "--target-edge-length" : "--cell-size")
                  << " to >= " << safe << " mm (voxel size is " << vmin << " mm).\n";
        return 4;
    }

    Mesh_domain domain = Mesh_domain::create_labeled_image_mesh_domain(image);

    // Mesh_criteria_3 uses Boost.Parameter keywords (keyword = value), unlike
    // make_mesh_3's chained named parameters below.
    Mesh_criteria criteria(params::facet_angle = o.facet_angle, params::facet_size = o.facet_size,
                           params::facet_distance = o.facet_distance,
                           params::cell_radius_edge_ratio = o.cell_ratio,
                           params::cell_size = o.cell_size);

    std::cout << "  meshing (Mesh_3)...\n";
    C3t3 c3t3 = o.manifold ? CGAL::make_mesh_3<C3t3>(domain, criteria, params::manifold())
                           : CGAL::make_mesh_3<C3t3>(domain, criteria);

    std::cout << "  after meshing: cells_in_complex=" << c3t3.number_of_cells_in_complex()
              << " vertices=" << c3t3.triangulation().number_of_vertices() << "\n";
    const auto hist_mesh = subdomain_histogram(c3t3);
    print_hist("meshed", hist_mesh);

    if (o.remesh) {
        std::cout << "  tetrahedral isotropic remeshing: target_edge=" << o.target_edge
                  << " iterations=" << o.iterations << " ...\n";
        CGAL::tetrahedral_isotropic_remeshing(
            c3t3, o.target_edge,
            params::number_of_iterations(o.iterations).remesh_boundaries(o.remesh_boundaries));
        std::cout << "  after remeshing: cells_in_complex=" << c3t3.number_of_cells_in_complex()
                  << " vertices=" << c3t3.triangulation().number_of_vertices() << "\n";
        print_hist("remeshed", subdomain_histogram(c3t3));
    }

    std::ofstream out(o.out_path);
    if (!out) {
        std::cerr << "ERROR: cannot open output: " << o.out_path << "\n";
        return 2;
    }
    c3t3.output_to_medit(out);
    std::cout << "  wrote: " << o.out_path << "\n";
    return 0;
}
