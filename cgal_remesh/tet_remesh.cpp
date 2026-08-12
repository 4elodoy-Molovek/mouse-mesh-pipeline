// ============================================================================
//  tet_remesh — multi-material tetrahedral isotropic remeshing (CGAL)
// ----------------------------------------------------------------------------
//  Purpose
//  -------
//  The pipeline's conformal CGAL tet mesh (brain_full_conformal.mesh, Medit
//  format with per-tet subdomain refs) is watertight and conformal, but its
//  *surface* carries irregular oversized triangles — the "pimples"/spikes the
//  advisor objected to. Per-tissue surface smoothing cannot fix this without
//  breaking inter-tissue conformality, because neighbouring tissues share the
//  exact same interface vertices (skin ⊃ skull ⊃ … ⊃ cerebrum): moving one
//  tissue's copy of a shared wall independently reopens gaps.
//
//  CGAL's Tetrahedral Remeshing operates on the *whole* multi-domain
//  triangulation at once. It splits/collapses/flips edges and relocates
//  vertices to make elements isotropic (uniform, well-shaped), INCLUDING the
//  boundary surface (tangential smoothing that removes spikes) while preserving
//  every subdomain interface by construction. Input and output are both Medit
//  .mesh with subdomain refs, so the result is a drop-in replacement that stays
//  a valid, conformal, multi-material MMC mesh — no re-tetrahedralisation.
//
//  I/O
//  ---
//    read_MEDIT  : reads Vertices, Triangles (surface patch refs) and
//                  Tetrahedra (subdomain refs) into a Remeshing_triangulation_3.
//    write_MEDIT : writes the remeshed triangulation back to Medit, preserving
//                  the subdomain ref integers (the pipeline's downstream Python
//                  already remaps CGAL's sequential subdomain ids back to the
//                  true tissue labels).
//
//  Usage
//  -----
//    tet_remesh <in.mesh> <out.mesh>
//               [--target-edge-length L]   (mm; default = mean input edge)
//               [--iterations N]           (default 3)
//               [--freeze-boundaries]      (remesh interior only, keep surface)
//               [--smooth-features]        (also relax triple-line polylines)
//
//  Build: see CMakeLists.txt / build.ps1 in this folder.
// ============================================================================

#include <CGAL/Exact_predicates_inexact_constructions_kernel.h>
#include <CGAL/Tetrahedral_remeshing/Remeshing_triangulation_3.h>
#include <CGAL/tetrahedral_remeshing.h>
#include <CGAL/IO/File_medit.h>

#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <string>

typedef CGAL::Exact_predicates_inexact_constructions_kernel K;
typedef CGAL::Tetrahedral_remeshing::Remeshing_triangulation_3<K> Remeshing_triangulation;
typedef Remeshing_triangulation::Point Point;

namespace {

struct Options {
    std::string in_path;
    std::string out_path;
    double target_edge_length = 0.0; // 0 => auto (mean input edge)
    int iterations = 3;
    bool remesh_boundaries = true;
    bool smooth_features = false;
};

[[noreturn]] void usage_and_exit(const char* prog, int code) {
    std::cerr << "Usage: " << prog
              << " <in.mesh> <out.mesh> [options]\n"
                 "  --target-edge-length L   target edge length in mm (default: mean input edge)\n"
                 "  --iterations N           remeshing sweeps (default 3)\n"
                 "  --freeze-boundaries      remesh interior only, keep the surface fixed\n"
                 "  --smooth-features        also relax triple-line polyline constraints\n";
    std::exit(code);
}

Options parse_args(int argc, char** argv) {
    if (argc < 3) usage_and_exit(argv[0], 1);
    Options o;
    o.in_path = argv[1];
    o.out_path = argv[2];
    for (int i = 3; i < argc; ++i) {
        const std::string a = argv[i];
        auto need_value = [&](const char* name) -> const char* {
            if (i + 1 >= argc) {
                std::cerr << "Missing value for " << name << "\n";
                usage_and_exit(argv[0], 1);
            }
            return argv[++i];
        };
        if (a == "--target-edge-length")
            o.target_edge_length = std::atof(need_value("--target-edge-length"));
        else if (a == "--iterations")
            o.iterations = std::atoi(need_value("--iterations"));
        else if (a == "--freeze-boundaries")
            o.remesh_boundaries = false;
        else if (a == "--smooth-features")
            o.smooth_features = true;
        else if (a == "-h" || a == "--help")
            usage_and_exit(argv[0], 0);
        else {
            std::cerr << "Unknown argument: " << a << "\n";
            usage_and_exit(argv[0], 1);
        }
    }
    return o;
}

// Mean length of the finite edges of the triangulation — used as the default
// isotropic target so the remesh keeps roughly the input resolution.
double mean_finite_edge_length(const Remeshing_triangulation& tr) {
    double sum = 0.0;
    std::size_t n = 0;
    for (auto e = tr.finite_edges_begin(); e != tr.finite_edges_end(); ++e) {
        const Point& p = e->first->vertex(e->second)->point();
        const Point& q = e->first->vertex(e->third)->point();
        sum += std::sqrt(CGAL::squared_distance(p, q));
        ++n;
    }
    return (n == 0) ? 0.0 : sum / static_cast<double>(n);
}

// Count cells per subdomain (finite, in-domain cells only) for a before/after
// material report — a cheap sanity check that no tissue vanished.
std::map<int, std::size_t> subdomain_histogram(const Remeshing_triangulation& tr) {
    std::map<int, std::size_t> h;
    for (auto c = tr.finite_cells_begin(); c != tr.finite_cells_end(); ++c) {
        const int s = c->subdomain_index();
        if (s != 0) ++h[s];
    }
    return h;
}

void print_histogram(const char* tag, const std::map<int, std::size_t>& h) {
    std::cout << "  " << tag << " subdomains: ";
    for (auto it = h.begin(); it != h.end(); ++it)
        std::cout << it->first << "=" << it->second << "  ";
    std::cout << "\n";
}

} // namespace

int main(int argc, char** argv) {
    const Options opt = parse_args(argc, argv);

    Remeshing_triangulation tr;
    {
        std::ifstream in(opt.in_path);
        if (!in) {
            std::cerr << "ERROR: cannot open input: " << opt.in_path << "\n";
            return 2;
        }
        if (!CGAL::IO::read_MEDIT(in, tr)) {
            std::cerr << "ERROR: read_MEDIT failed on: " << opt.in_path << "\n";
            return 2;
        }
    }

    const std::size_t nv_in = tr.number_of_vertices();
    const std::size_t nc_in = tr.number_of_finite_cells();
    const auto hist_in = subdomain_histogram(tr);
    const double mean_edge = mean_finite_edge_length(tr);

    double target = opt.target_edge_length;
    if (target <= 0.0) target = mean_edge;

    std::cout << "=== tet_remesh ===\n";
    std::cout << "  input:  " << opt.in_path << "\n";
    std::cout << "  output: " << opt.out_path << "\n";
    std::cout << "  vertices=" << nv_in << "  finite_cells=" << nc_in << "\n";
    std::cout << "  mean input edge = " << mean_edge << " mm\n";
    std::cout << "  target edge length = " << target << " mm"
              << (opt.target_edge_length <= 0.0 ? " (auto)\n" : "\n");
    std::cout << "  iterations=" << opt.iterations
              << "  remesh_boundaries=" << (opt.remesh_boundaries ? "true" : "false")
              << "  smooth_features=" << (opt.smooth_features ? "true" : "false") << "\n";
    print_histogram("input", hist_in);

    if (target <= 0.0) {
        std::cerr << "ERROR: could not determine a positive target edge length.\n";
        return 2;
    }

    namespace np = CGAL::parameters;
    CGAL::tetrahedral_isotropic_remeshing(tr, target,
                                          np::number_of_iterations(opt.iterations)
                                              .remesh_boundaries(opt.remesh_boundaries)
                                              .smooth_constrained_edges(opt.smooth_features));

    const std::size_t nv_out = tr.number_of_vertices();
    const std::size_t nc_out = tr.number_of_finite_cells();
    const auto hist_out = subdomain_histogram(tr);

    std::cout << "  --- after remeshing ---\n";
    std::cout << "  vertices=" << nv_out << "  finite_cells=" << nc_out << "\n";
    print_histogram("output", hist_out);

    // Guard: every input subdomain must survive (no tissue silently deleted).
    bool ok = true;
    for (auto it = hist_in.begin(); it != hist_in.end(); ++it) {
        if (hist_out.find(it->first) == hist_out.end()) {
            std::cerr << "WARNING: subdomain " << it->first << " disappeared during remeshing!\n";
            ok = false;
        }
    }

    {
        std::ofstream out(opt.out_path);
        if (!out) {
            std::cerr << "ERROR: cannot open output: " << opt.out_path << "\n";
            return 2;
        }
        CGAL::IO::write_MEDIT(out, tr);
    }
    std::cout << "  wrote: " << opt.out_path << "\n";
    return ok ? 0 : 3;
}
