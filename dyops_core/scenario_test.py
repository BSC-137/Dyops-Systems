import numpy as np
import matplotlib.pyplot as plt
from dyops_core import BasisObserver # Change to vex_core if you haven't renamed the crate
import time
import os

def run_scenario():
    # 1. Observer Parameters
    theta = 1.2             # Mean-reversion speed
    m_noise = 0.000005  # Tighten measurement noise (Trust the raw data)
    q_basis = 1e-9      # Reduce basis uncertainty
    q_vel = 1e-8    # Measurement noise (Scalar R)
    
    
    q_mean = 1e-8
    
    p_noise = [
        q_basis, 0.0,   0.0,
        0.0,     q_vel, 0.0,
        0.0,     0.0,   q_mean
    ]
    
    # Initialize the Engine
    observer = BasisObserver(
        name="Dyops-Alpha-Sentinel",
        theta=theta,
        process_noise=p_noise,
        measurement_noise=m_noise,
        ring_buffer_capacity=1500
    )

    # 2. Generate Synthetic "Market" Data (2500 ticks)
    n_ticks = 2500
    t = np.linspace(0, 100, n_ticks)
    
    # Physical Asset: Random walk
    physical_price = 100.0 + np.cumsum(np.random.normal(0, 0.05, n_ticks))
    
    # Tokenized Asset: Simulated regimes
    token_price = physical_price.copy()
    
    # Regime A: Stable Tracking (0-1000)
    token_price[0:1000] += np.random.normal(0, 0.005, 1000)
    
    # Regime B: The "Slow Decay" (1000-1800) - 50bps drift
    token_price[1000:1800] += np.linspace(0, -0.5, 800) + np.random.normal(0, 0.005, 800)
    
    # Regime C: The "Structural Break" (1800-2500) - Sudden 2% drop
    token_price[1800:] -= 2.0 + np.random.normal(0, 0.08, 700)

    # 3. Process Batch through Rust Engine
    print(f"🛰️  Dyops Systems: Sending {n_ticks} ticks to Rust core...")
    start_ts = time.perf_counter()
    results = observer.update_batch(t, physical_price, token_price)
    duration = (time.perf_counter() - start_ts) * 1000
    print(f"✅ Batch complete in {duration:.2f}ms")

    # 4. Extract Diagnostics
    f_basis = results['filtered_basis']
    innov = results['innovation']
    mahal = results['mahalanobis_distance']
    
    stats = observer.get_window_stats()
    crit_score = observer.get_criticality_score()

    # 5. Visualization (The "Institutional" Dashboard View)
    plt.style.use('dark_background')
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
    fig.suptitle(f"Dyops Basis Guard: {observer.name} Diagnostic Report", color='gold', fontsize=14, fontweight='bold')

    # Top Plot: Price Tracking
    ax1.plot(t, physical_price, label="Physical (Reference)", color='#00d4ff', alpha=0.8)
    ax1.plot(t, token_price, label="Tokenized Asset", color='#ff00ff', alpha=0.8)
    ax1.set_ylabel("USD Price")
    ax1.legend(loc='upper left')
    ax1.grid(alpha=0.1)

    # Middle Plot: Innovation (The 'Noise' Filter)
    ax2.plot(t, innov, label="Innovation (Prediction Error)", color='#ffff00', lw=1)
    ax2.fill_between(t, innov, color='#ffff00', alpha=0.1)
    ax2.axhline(0, color='white', ls='--', alpha=0.3)
    ax2.set_ylabel("Innovation")
    ax2.legend(loc='upper left')
    ax2.grid(alpha=0.1)

    # Bottom Plot: Mahalanobis Anomaly Detection
    ax3.plot(t, mahal, label="Criticality (Mahalanobis Distance)", color='#ff3131', lw=1.5)
    ax3.axhline(3.0, color='orange', ls='--', label="Institutional Alarm (3σ)")
    ax3.set_ylabel("Sigma Level")
    ax3.set_xlabel("Time (Ticks)")
    ax3.legend(loc='upper left')
    ax3.grid(alpha=0.1)

    plt.tight_layout()
    
    # Save for WSL and show
    output_path = "dyops_diagnostic.png"
    plt.savefig(output_path)
    print(f"📊 Diagnostic plot saved to: {os.path.abspath(output_path)}")
    
    try:
        plt.show()
    except Exception:
        print("💡 Note: Display not detected. Use the saved .png to view results.")

    # 6. Final Telemetry Report
    print("-" * 45)
    print(f"DYOPS TELEMETRY SUMMARY")
    print(f"System State:   {'🔴 ALARM' if crit_score > 15 else '🟢 STABLE'}")
    print(f"Criticality:    {crit_score:.2f}%")
    print(f"Inno. Kurtosis: {stats.kurtosis:.2f} (Tail Risk)")
    print("-" * 45)

if __name__ == "__main__":
    run_scenario()