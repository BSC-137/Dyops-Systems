use dyops_core::{
    BasisObserver, ObserverInit, SentinelPolicy, AUDIT_COOLDOWN_TICKS, CRITICALITY_AUDIT_PCT,
    CRITICALITY_WINDOW,
};
use std::alloc::{GlobalAlloc, Layout, System};
use std::hint::black_box;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

struct CountingAllocator;

static ALLOCATIONS: AtomicU64 = AtomicU64::new(0);

unsafe impl GlobalAlloc for CountingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
        System.alloc(layout)
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        System.dealloc(ptr, layout);
    }
}

#[global_allocator]
static GLOBAL_ALLOCATOR: CountingAllocator = CountingAllocator;

fn observer(name: &str, ring_capacity: usize) -> BasisObserver {
    BasisObserver::new(ObserverInit {
        name: name.into(),
        theta: 1.0,
        q_process: None,
        r_measurement: None,
        ring_buffer_capacity: Some(ring_capacity),
    })
    .expect("valid benchmark observer")
}

#[inline]
fn prices(tick: usize) -> (f64, f64) {
    let offset = (tick % 17) as f64 - 8.0;
    (100.0 + offset * 1e-5, 100.0)
}

fn measure<F>(name: &str, iterations: usize, warmup: usize, mut tick: F)
where
    F: FnMut(usize) -> f64,
{
    let mut checksum = 0.0;
    for i in 0..warmup {
        checksum += black_box(tick(i));
    }
    let allocations_before = ALLOCATIONS.load(Ordering::Relaxed);
    let start = Instant::now();
    for i in warmup..warmup + iterations {
        checksum += black_box(tick(i));
    }
    let elapsed = start.elapsed();
    let allocations = ALLOCATIONS
        .load(Ordering::Relaxed)
        .saturating_sub(allocations_before);
    let total_ns = elapsed.as_nanos();
    let ns_per_tick = total_ns as f64 / iterations as f64;
    let ticks_per_sec = 1e9 / ns_per_tick;
    println!(
        "{name},{iterations},{total_ns},{ns_per_tick:.3},{ticks_per_sec:.0},{allocations},{checksum:.9}"
    );
}

fn main() {
    let iterations = std::env::var("DYOPS_BENCH_TICKS")
        .ok()
        .and_then(|raw| raw.parse().ok())
        .unwrap_or(1_000_000);
    let audit_iterations = std::env::var("DYOPS_BENCH_AUDIT_TICKS")
        .ok()
        .and_then(|raw| raw.parse().ok())
        .unwrap_or(100_000);
    let warmup = 10_000;

    println!("case,iterations,total_ns,ns_per_tick,ticks_per_sec,allocations,checksum");

    let mut ring_off = observer("bench-ring-off", 0);
    measure("observer_monitoring_ring_off", iterations, warmup, |i| {
        let (physical, token) = prices(i);
        ring_off
            .update(i as f64 * 0.001, physical, token)
            .filtered_basis
    });

    let mut ring_on = observer("bench-ring-on", 1000);
    measure("observer_monitoring_ring_on", iterations, warmup, |i| {
        let (physical, token) = prices(i);
        ring_on
            .update(i as f64 * 0.001, physical, token)
            .filtered_basis
    });

    let mut monitoring = SentinelPolicy::new(
        observer("bench-sentinel-monitoring", 1000),
        CRITICALITY_WINDOW,
        CRITICALITY_AUDIT_PCT,
        AUDIT_COOLDOWN_TICKS,
    )
    .expect("valid monitoring policy");
    measure("sentinel_monitoring", iterations, warmup, |i| {
        let (physical, token) = prices(i);
        let event = monitoring.process_event(i as f64 * 0.001, physical, token);
        event.health.filtered_basis + event.criticality_recent_pct
    });

    let mut audit = SentinelPolicy::new(
        observer("bench-sentinel-audit", 1000),
        CRITICALITY_WINDOW,
        -1.0,
        0,
    )
    .expect("valid audit policy");
    measure(
        "sentinel_audit_snapshot_tick",
        audit_iterations,
        warmup,
        |i| {
            let (physical, token) = prices(i);
            let event = audit.process_event(i as f64 * 0.001, physical, token);
            event
                .snapshot
                .as_ref()
                .map_or(0.0, |snapshot| snapshot.window_metrics.mean)
        },
    );
}
