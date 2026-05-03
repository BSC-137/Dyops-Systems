//! Discrete-time Kalman observer for a mean-reverting basis with Joseph-form covariance.

use nalgebra::{Matrix3, RowVector3, Vector3};
use std::collections::VecDeque;

/// Output of one filter step: filtered estimate, innovation, and normalized surprise (Mahalanobis).
#[derive(Clone, Debug, PartialEq)]
pub struct SystemHealth {
    /// Posterior estimate of basis (log-ratio) after the update.
    pub filtered_basis: f64,
    /// Innovation `y = z - H x_{pred}` (measurement minus predicted observation).
    pub innovation: f64,
    /// Scalar Mahalanobis distance `|y| / sqrt(S)` with `S = H P_{pred} H^T + R` (undefined → 0).
    pub mahalanobis_distance: f64,
    /// Whether this update used a finite, strictly positive price pair.
    pub measurement_valid: bool,
}

/// Rolling window statistics over innovations (Fisher excess kurtosis where defined).
#[derive(Clone, Debug, PartialEq)]
pub struct WindowStats {
    pub mean: f64,
    pub variance: f64,
    pub kurtosis: f64,
}

#[derive(Clone, Copy, Debug)]
struct RingSample {
    /// Retained for diagnostic series (e.g. future time-aligned export).
    #[allow(dead_code)]
    timestamp: f64,
    innovation: f64,
    mahalanobis: f64,
}

/// Constructor configuration for [`BasisObserver`].
#[derive(Clone, Debug)]
pub struct ObserverInit {
    pub name: String,
    /// Mean-reversion speed `θ > 0` (OU time scale).
    pub theta: f64,
    /// Row-major 3×3 process noise covariance `Q` (optional; defaults to diagonal small noise).
    pub q_process: Option<[f64; 9]>,
    /// Scalar measurement noise variance `R > 0` (optional).
    pub r_measurement: Option<f64>,
    /// Ring buffer capacity for diagnostics. `None` → 1000. `Some(0)` disables the ring.
    pub ring_buffer_capacity: Option<usize>,
}

/// State-space observer: basis (log physical / token), velocity, mean level — critically damped OU discretization.
pub struct BasisObserver {
    name: String,
    theta: f64,
    /// State x = [basis, velocity, mean_level]
    x: Vector3<f64>,
    p: Matrix3<f64>,
    phi: Matrix3<f64>,
    h: RowVector3<f64>,
    q: Matrix3<f64>,
    r: f64,
    last_t: Option<f64>,
    initialized: bool,
    ring_cap: usize,
    ring: VecDeque<RingSample>,
}

impl BasisObserver {
    pub fn new(init: ObserverInit) -> Result<Self, String> {
        if !(init.theta.is_finite()) || init.theta <= 0.0 {
            return Err("theta must be finite and positive".into());
        }
        let r = init.r_measurement.unwrap_or(1e-6_f64);
        if !(r.is_finite()) || r <= 0.0 {
            return Err("measurement noise variance R must be finite and positive".into());
        }

        let name = init.name;
        let theta = init.theta;
        let q = if let Some(qf) = init.q_process {
            Matrix3::new(
                qf[0], qf[1], qf[2],
                qf[3], qf[4], qf[5],
                qf[6], qf[7], qf[8],
            )
        } else {
            Matrix3::from_diagonal(&Vector3::new(1e-8_f64, 1e-6_f64, 1e-10_f64))
        };

        let ring_cap = init.ring_buffer_capacity.unwrap_or(1000);

        Ok(Self {
            name,
            theta,
            x: Vector3::zeros(),
            p: Matrix3::identity() * 1e4_f64,
            phi: Matrix3::identity(),
            h: RowVector3::new(1.0, 0.0, 0.0),
            q,
            r,
            last_t: None,
            initialized: false,
            ring_cap,
            ring: VecDeque::new(),
        })
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    /// Sample excess kurtosis (Fisher, bias-adjusted) on innovations; NaNs if undefined.
    pub fn window_stats(&self) -> WindowStats {
        let n = self.ring.len();
        if n == 0 {
            return WindowStats {
                mean: f64::NAN,
                variance: f64::NAN,
                kurtosis: f64::NAN,
            };
        }
        let mut sum = 0.0;
        for s in &self.ring {
            sum += s.innovation;
        }
        let nf = n as f64;
        let mean = sum / nf;
        if n == 1 {
            return WindowStats {
                mean,
                variance: f64::NAN,
                kurtosis: f64::NAN,
            };
        }
        let mut sq = 0.0;
        for s in &self.ring {
            let d = s.innovation - mean;
            sq += d * d;
        }
        let variance = sq / (nf - 1.0);
        let kurtosis = if n < 4 || variance <= 0.0 || !variance.is_finite() {
            f64::NAN
        } else {
            let s = variance.sqrt();
            let mut sum_z4 = 0.0;
            for x in self.ring.iter().map(|r| r.innovation) {
                let z = (x - mean) / s;
                sum_z4 += z * z * z * z;
            }
            let n64 = nf;
            let num = n64 * (n64 + 1.0);
            let den = (n64 - 1.0) * (n64 - 2.0) * (n64 - 3.0);
            let adj = 3.0 * (n64 - 1.0).powi(2) / ((n64 - 2.0) * (n64 - 3.0));
            (num / den) * sum_z4 - adj
        };
        WindowStats {
            mean,
            variance,
            kurtosis,
        }
    }

    /// Percent of ring samples with Mahalanobis distance strictly greater than 3.0.
    pub fn criticality_score(&self) -> f64 {
        if self.ring.is_empty() || self.ring_cap == 0 {
            return 0.0;
        }
        let hi = self
            .ring
            .iter()
            .filter(|s| s.mahalanobis > 3.0)
            .count();
        100.0 * (hi as f64) / (self.ring.len() as f64)
    }

    /// Up to `n` most recent innovation values (chronological order: oldest → newest in the returned slice).
    pub fn last_innovations(&self, n: usize) -> Vec<f64> {
        if n == 0 || self.ring.is_empty() {
            return Vec::new();
        }
        let take = n.min(self.ring.len());
        let skip = self.ring.len() - take;
        self.ring
            .iter()
            .skip(skip)
            .map(|s| s.innovation)
            .collect()
    }

    /// Posterior filtered basis and velocity state components (`b`, `v` from `x = [b, v, μ]`).
    pub fn basis_velocity(&self) -> (f64, f64) {
        (self.x[0], self.x[1])
    }

    /// `%` of samples with Mahalanobis > 3 among the last `window` ring entries (full buffer if shorter).
    pub fn criticality_recent(&self, window: usize) -> f64 {
        if window == 0 || self.ring.is_empty() {
            return 0.0;
        }
        let w = window.min(self.ring.len());
        let skip = self.ring.len() - w;
        let hi = self
            .ring
            .iter()
            .skip(skip)
            .filter(|s| s.mahalanobis > 3.0)
            .count();
        100.0 * (hi as f64) / (w as f64)
    }

    /// One Kalman step. Invalid prices yield `measurement_valid: false` and do not mutate the filter.
    ///
    /// Implemented in Rust so this hot path avoids GC pauses: per-tick cost stays predictable under
    /// continuous ingestion, which stabilizes latency for timely state during volatile basis moves.
    pub fn update(&mut self, timestamp: f64, physical_price: f64, token_price: f64) -> SystemHealth {
        let h = self.compute_update(timestamp, physical_price, token_price);
        self.push_ring(timestamp, &h);
        h
    }

    /// Batch update over parallel slices (same robustness as [`Self::update`] per tick).
    pub fn update_batch_slice(
        &mut self,
        timestamps: &[f64],
        physical: &[f64],
        token: &[f64],
    ) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
        let n = timestamps.len();
        debug_assert_eq!(n, physical.len());
        debug_assert_eq!(n, token.len());
        let mut filtered_basis = Vec::with_capacity(n);
        let mut innovation = Vec::with_capacity(n);
        let mut mahalanobis_distance = Vec::with_capacity(n);
        for i in 0..n {
            let h = self.compute_update(timestamps[i], physical[i], token[i]);
            self.push_ring(timestamps[i], &h);
            filtered_basis.push(h.filtered_basis);
            innovation.push(h.innovation);
            mahalanobis_distance.push(h.mahalanobis_distance);
        }
        (
            filtered_basis,
            innovation,
            mahalanobis_distance,
        )
    }

    fn push_ring(&mut self, timestamp: f64, h: &SystemHealth) {
        if self.ring_cap == 0 {
            return;
        }
        while self.ring.len() >= self.ring_cap {
            self.ring.pop_front();
        }
        self.ring.push_back(RingSample {
            timestamp,
            innovation: h.innovation,
            mahalanobis: h.mahalanobis_distance,
        });
    }

    fn compute_update(
        &mut self,
        timestamp: f64,
        physical_price: f64,
        token_price: f64,
    ) -> SystemHealth {
        let last_b = self.x[0];
        if !timestamp.is_finite() {
            return invalid_health(last_b);
        }

        if !prices_acceptable(physical_price, token_price) {
            return invalid_health(last_b);
        }

        let z = (physical_price / token_price).ln();
        if !z.is_finite() {
            return invalid_health(last_b);
        }

        if !self.initialized {
            self.last_t = Some(timestamp);
            self.x = Vector3::new(z, 0.0, z);
            self.p = Matrix3::from_diagonal(&Vector3::new(
                self.r * 10.0,
                self.r * 100.0,
                self.r * 10.0,
            ));
            self.initialized = true;
            return SystemHealth {
                filtered_basis: z,
                innovation: 0.0,
                mahalanobis_distance: 0.0,
                measurement_valid: true,
            };
        }

        let dt_raw = self
            .last_t
            .map(|prev| timestamp - prev)
            .filter(|d| d.is_finite())
            .unwrap_or(0.0);
        let dt = if dt_raw > 0.0 { dt_raw } else { 0.0 };
        self.last_t = Some(timestamp);

        self.phi = if dt > 0.0 {
            phi_matrix(self.theta, dt)
        } else {
            Matrix3::identity()
        };

        // Predict
        let x_pred = self.phi * self.x;
        let p_pred = self.phi * self.p * self.phi.transpose() + self.q;

        // Update (linear measurement H = [1,0,0])
        let hx = (self.h * x_pred)[0];
        let y_scalar = z - hx;

        // S = H P H^T + R  (scalar)
        let hpht = (self.h * p_pred * self.h.transpose())[0];
        let s = hpht + self.r;

        if s <= 0.0 || !s.is_finite() {
            return invalid_health(last_b);
        }

        // Kalman gain K (3×1)
        let k = p_pred * self.h.transpose() * (1.0 / s);

        let i = Matrix3::identity();
        let kh = outer_kw(&k, &self.h);
        let a = i - kh;

        // Joseph-form covariance update: P = (I-KH) P_pred (I-KH)^T + K R K^T.
        // Scalar R: K R K^T = R * (K K^T).
        let p_post = a * p_pred * a.transpose() + k * k.transpose() * self.r;

        let x_post = x_pred + k * y_scalar;

        self.x = x_post;
        self.p = symmetrize_psd(p_post);

        let s_pos = s.max(1e-300);
        let mahal = y_scalar.abs() / s_pos.sqrt();

        SystemHealth {
            filtered_basis: self.x[0],
            innovation: y_scalar,
            mahalanobis_distance: mahal,
            measurement_valid: true,
        }
    }
}

fn invalid_health(last_basis: f64) -> SystemHealth {
    SystemHealth {
        filtered_basis: last_basis,
        innovation: 0.0,
        mahalanobis_distance: 0.0,
        measurement_valid: false,
    }
}

fn prices_acceptable(p_phys: f64, p_tok: f64) -> bool {
    if !(p_phys.is_finite() && p_tok.is_finite()) {
        return false;
    }
    if p_phys <= 0.0 || p_tok <= 0.0 {
        return false;
    }
    true
}

/// Exact discrete `Φ(Δ)` for critically damped OU on `[basis, velocity]` with mean level state.
///
/// Continuous skeleton: `d[b]/dt = v`, `d[v]/dt = -θ²(b - μ) - 2θ v`, `dμ/dt = 0`, then exact exp on `[b-μ, v]`.
fn phi_matrix(theta: f64, dt: f64) -> Matrix3<f64> {
    if !(theta.is_finite() && dt.is_finite()) || dt <= 0.0 {
        return Matrix3::identity();
    }

    let a = -theta * dt;
    let e = a.exp();

    // N = [[θ, 1], [-θ², -θ]], N² = 0  ⇒  exp(M Δ) = e^{-θΔ} (I + Δ N)
    let e11 = e * (1.0 + dt * theta);
    let e12 = e * dt;
    let e21 = e * (-dt * theta * theta);
    let e22 = e * (1.0 - dt * theta);

    Matrix3::new(
        e11, e12, 1.0 - e11,
        e21, e22, -e21,
        0.0, 0.0, 1.0,
    )
}

fn symmetrize_psd(p: Matrix3<f64>) -> Matrix3<f64> {
    0.5 * (p + p.transpose())
}

#[inline]
fn outer_kw(k: &Vector3<f64>, h: &RowVector3<f64>) -> Matrix3<f64> {
    Matrix3::new(
        k[0] * h[0],
        k[0] * h[1],
        k[0] * h[2],
        k[1] * h[0],
        k[1] * h[1],
        k[1] * h[2],
        k[2] * h[0],
        k[2] * h[1],
        k[2] * h[2],
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phi_row_sums_mean_level() {
        let th = 0.5;
        let dt = 0.1;
        let phi = phi_matrix(th, dt);
        let x = Vector3::new(0.1, 0.02, 1.0);
        let xp = phi * x;
        assert!((xp[2] - 1.0).abs() < 1e-12);
    }

    #[test]
    fn joseph_update_no_panic_and_psd() {
        let init = ObserverInit {
            name: "t".into(),
            theta: 1.0,
            q_process: None,
            r_measurement: Some(1e-4),
            ring_buffer_capacity: Some(1000),
        };
        let mut o = BasisObserver::new(init).unwrap();
        o.update(0.0, 100.0, 100.0);
        o.update(1.0, 101.0, 100.0);
        let h = o.update(2.0, 100.5, 100.2);
        assert!(h.measurement_valid);
        assert!(h.filtered_basis.is_finite());
    }

    #[test]
    fn zero_token_no_panic() {
        let init = ObserverInit {
            name: "t".into(),
            theta: 0.2,
            q_process: None,
            r_measurement: None,
            ring_buffer_capacity: None,
        };
        let mut o = BasisObserver::new(init).unwrap();
        let h = o.update(0.0, 1.0, 0.0);
        assert!(!h.measurement_valid);
    }

    #[test]
    fn batch_matches_serial() {
        let init = ObserverInit {
            name: "b".into(),
            theta: 0.7,
            q_process: None,
            r_measurement: Some(1e-5),
            ring_buffer_capacity: Some(0),
        };
        let mut serial_obs = BasisObserver::new(init.clone()).unwrap();
        let t = [0.0_f64, 0.1, 0.25, 0.25, 0.4];
        let p = [100.0_f64, 100.2, 100.0, 100.0, 99.0];
        let k = [100.0_f64, 100.0, 100.5, 0.0, 100.0];
        let mut ser_fb = Vec::new();
        let mut ser_inn = Vec::new();
        let mut ser_m = Vec::new();
        for i in 0..t.len() {
            let h = serial_obs.update(t[i], p[i], k[i]);
            ser_fb.push(h.filtered_basis);
            ser_inn.push(h.innovation);
            ser_m.push(h.mahalanobis_distance);
        }
        let mut batch_obs = BasisObserver::new(init).unwrap();
        let (fb, inn, m) = batch_obs.update_batch_slice(&t, &p, &k);
        assert_eq!(ser_fb, fb);
        assert_eq!(ser_inn, inn);
        assert_eq!(ser_m, m);
    }

    #[test]
    fn criticality_recent_emphasizes_tail() {
        let init = ObserverInit {
            name: "r".into(),
            theta: 1.0,
            q_process: None,
            r_measurement: Some(1e-6),
            ring_buffer_capacity: Some(20),
        };
        let mut o = BasisObserver::new(init).unwrap();
        for i in 0..15 {
            o.update(i as f64, 100.0, 100.0);
        }
        for i in 15..20 {
            o.update(i as f64, 150.0, 100.0);
        }
        assert!(o.criticality_recent(5) > 50.0);
        assert!(o.criticality_recent(20) < o.criticality_recent(5));
    }

    #[test]
    fn criticality_reflects_noise_tail() {
        let init = ObserverInit {
            name: "c".into(),
            theta: 2.0,
            q_process: None,
            r_measurement: Some(1e-8),
            ring_buffer_capacity: Some(1000),
        };
        let mut o = BasisObserver::new(init).unwrap();
        let mut t = 0.0_f64;
        for _ in 0..2000 {
            t += 1.0;
            o.update(t, 100.0, 100.0);
        }
        for _ in 0..50 {
            t += 1.0;
            // Large basis shock vs steady 1.0 ratio
            o.update(t, 130.0, 100.0);
        }
        let crit = o.criticality_score();
        // Last 1000 ticks: 50 noisy + 950 calm → ~5% above threshold (depends on filter Mahalanobis)
        assert!(crit > 1.0 && crit < 15.0, "crit={crit}");
        let ws = o.window_stats();
        assert!(ws.mean.is_finite());
    }
}
