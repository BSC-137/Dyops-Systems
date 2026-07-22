//! Discrete-time Kalman observer for a mean-reverting basis with Joseph-form covariance.

use nalgebra::{Matrix3, Vector3};

use crate::sentinel::MAHALANOBIS_BREACH;

const PHI_DT_EPSILON: f64 = f64::EPSILON * 8.0;

/// Output of one filter step: filtered estimate, innovation, and normalized surprise (Mahalanobis).
#[derive(Clone, Copy, Debug, PartialEq)]
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
    innovation: f64,
    breach: bool,
    breach_prefix: u64,
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
#[derive(Clone)]
pub struct BasisObserver {
    name: String,
    theta: f64,
    /// State x = [basis, velocity, mean_level]
    x: Vector3<f64>,
    p: Matrix3<f64>,
    phi: Matrix3<f64>,
    phi_dt: Option<f64>,
    q: Matrix3<f64>,
    r: f64,
    last_t: Option<f64>,
    initialized: bool,
    ring_cap: usize,
    ring: Vec<RingSample>,
    ring_head: usize,
    ring_breach_count: usize,
    breach_prefix_total: u64,
    breach_prefix_before_ring: u64,
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
                qf[0], qf[1], qf[2], qf[3], qf[4], qf[5], qf[6], qf[7], qf[8],
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
            phi_dt: None,
            q,
            r,
            last_t: None,
            initialized: false,
            ring_cap,
            ring: Vec::with_capacity(ring_cap),
            ring_head: 0,
            ring_breach_count: 0,
            breach_prefix_total: 0,
            breach_prefix_before_ring: 0,
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
        let mut sum1 = 0.0_f64;
        let mut sum2 = 0.0_f64;
        let mut sum3 = 0.0_f64;
        let mut sum4 = 0.0_f64;
        for sample in self.ring_iter() {
            let value = sample.innovation;
            let squared = value * value;
            sum1 += value;
            sum2 += squared;
            sum3 += squared * value;
            sum4 += squared * squared;
        }
        let nf = n as f64;
        let mean = sum1 / nf;
        if n == 1 {
            return WindowStats {
                mean,
                variance: f64::NAN,
                kurtosis: f64::NAN,
            };
        }
        let mean_squared = mean * mean;
        let centered_m2 = (sum2 - nf * mean_squared).max(0.0);
        let variance = centered_m2 / (nf - 1.0);
        let kurtosis = if n < 4 || variance <= 0.0 || !variance.is_finite() {
            f64::NAN
        } else {
            let centered_m4 = sum4 - 4.0 * mean * sum3 + 6.0 * mean_squared * sum2
                - 3.0 * nf * mean_squared * mean_squared;
            let sum_z4 = centered_m4 / (variance * variance);
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

    /// Percent of ring samples above the sentinel Mahalanobis breach threshold.
    pub fn criticality_score(&self) -> f64 {
        if self.ring.is_empty() || self.ring_cap == 0 {
            return 0.0;
        }
        100.0 * (self.ring_breach_count as f64) / (self.ring.len() as f64)
    }

    /// Up to `n` most recent innovation values (chronological order: oldest → newest in the returned slice).
    pub fn last_innovations(&self, n: usize) -> Vec<f64> {
        if n == 0 || self.ring.is_empty() {
            return Vec::new();
        }
        let take = n.min(self.ring.len());
        let skip = self.ring.len() - take;
        (skip..self.ring.len())
            .map(|index| self.ring_sample(index).innovation)
            .collect()
    }

    /// Posterior filtered basis and velocity state components (`b`, `v` from `x = [b, v, μ]`).
    pub fn basis_velocity(&self) -> (f64, f64) {
        (self.x[0], self.x[1])
    }

    /// `%` of samples above the sentinel breach threshold among the last `window` entries.
    pub fn criticality_recent(&self, window: usize) -> f64 {
        if window == 0 || self.ring.is_empty() {
            return 0.0;
        }
        let w = window.min(self.ring.len());
        let latest_prefix = self.ring_sample(self.ring.len() - 1).breach_prefix;
        let prefix_before_window = if self.ring.len() == w {
            self.breach_prefix_before_ring
        } else {
            self.ring_sample(self.ring.len() - w - 1).breach_prefix
        };
        let hi = latest_prefix.wrapping_sub(prefix_before_window) as usize;
        100.0 * (hi as f64) / (w as f64)
    }

    /// One Kalman step. Invalid prices yield `measurement_valid: false` and do not mutate the
    /// filter or the valid-observation diagnostics ring.
    ///
    /// Implemented in Rust so this hot path avoids GC pauses: per-tick cost stays predictable under
    /// continuous ingestion, which stabilizes latency for timely state during volatile basis moves.
    pub fn update(
        &mut self,
        timestamp: f64,
        physical_price: f64,
        token_price: f64,
    ) -> SystemHealth {
        let h = self.compute_update(timestamp, physical_price, token_price);
        if h.measurement_valid {
            self.push_ring(timestamp, &h);
        }
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
            if h.measurement_valid {
                self.push_ring(timestamps[i], &h);
            }
            filtered_basis.push(h.filtered_basis);
            innovation.push(h.innovation);
            mahalanobis_distance.push(h.mahalanobis_distance);
        }
        (filtered_basis, innovation, mahalanobis_distance)
    }

    fn push_ring(&mut self, _timestamp: f64, h: &SystemHealth) {
        if self.ring_cap == 0 {
            return;
        }
        let breach = h.mahalanobis_distance > MAHALANOBIS_BREACH;
        if breach {
            self.breach_prefix_total = self.breach_prefix_total.wrapping_add(1);
        }
        let sample = RingSample {
            innovation: h.innovation,
            breach,
            breach_prefix: self.breach_prefix_total,
        };
        if self.ring.len() < self.ring_cap {
            self.ring.push(sample);
        } else {
            let removed = self.ring[self.ring_head];
            self.breach_prefix_before_ring = removed.breach_prefix;
            if removed.breach {
                self.ring_breach_count -= 1;
            }
            self.ring[self.ring_head] = sample;
            self.ring_head = (self.ring_head + 1) % self.ring_cap;
        }
        if breach {
            self.ring_breach_count += 1;
        }
    }

    #[inline]
    fn ring_sample(&self, logical_index: usize) -> &RingSample {
        debug_assert!(logical_index < self.ring.len());
        if self.ring.len() < self.ring_cap {
            &self.ring[logical_index]
        } else {
            &self.ring[(self.ring_head + logical_index) % self.ring_cap]
        }
    }

    #[inline]
    fn ring_iter(
        &self,
    ) -> std::iter::Chain<std::slice::Iter<'_, RingSample>, std::slice::Iter<'_, RingSample>> {
        if self.ring.len() < self.ring_cap {
            self.ring.iter().chain(self.ring[0..0].iter())
        } else {
            self.ring[self.ring_head..]
                .iter()
                .chain(self.ring[..self.ring_head].iter())
        }
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
            self.p =
                Matrix3::from_diagonal(&Vector3::new(self.r * 10.0, self.r * 100.0, self.r * 10.0));
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

        let cached_dt_matches = self.phi_dt.is_some_and(|cached| {
            (cached - dt).abs() <= PHI_DT_EPSILON * cached.abs().max(dt.abs()).max(1.0)
        });
        if !cached_dt_matches {
            self.phi = if dt > 0.0 {
                phi_matrix(self.theta, dt)
            } else {
                Matrix3::identity()
            };
            self.phi_dt = Some(dt);
        }

        // Predict
        let x_pred = predict_state(&self.phi, &self.x);
        let p_pred = predict_covariance(&self.phi, &self.p, &self.q);

        // Update (linear measurement H = [1,0,0])
        let y_scalar = z - x_pred[0];

        // S = H P H^T + R  (scalar)
        let s = p_pred[(0, 0)] + self.r;

        if s <= 0.0 || !s.is_finite() {
            return invalid_health(last_b);
        }

        let inverse_s = 1.0 / s;
        let k0 = p_pred[(0, 0)] * inverse_s;
        let k1 = p_pred[(1, 0)] * inverse_s;
        let k2 = p_pred[(2, 0)] * inverse_s;
        let p_post = joseph_covariance(&p_pred, [k0, k1, k2], self.r);
        let x_post = Vector3::new(
            x_pred[0] + k0 * y_scalar,
            x_pred[1] + k1 * y_scalar,
            x_pred[2] + k2 * y_scalar,
        );

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

    Matrix3::new(e11, e12, 1.0 - e11, e21, e22, -e21, 0.0, 0.0, 1.0)
}

#[inline]
fn predict_state(phi: &Matrix3<f64>, x: &Vector3<f64>) -> Vector3<f64> {
    Vector3::new(
        phi[(0, 0)] * x[0] + phi[(0, 1)] * x[1] + phi[(0, 2)] * x[2],
        phi[(1, 0)] * x[0] + phi[(1, 1)] * x[1] + phi[(1, 2)] * x[2],
        phi[(2, 0)] * x[0] + phi[(2, 1)] * x[1] + phi[(2, 2)] * x[2],
    )
}

#[inline]
fn predict_covariance(phi: &Matrix3<f64>, p: &Matrix3<f64>, q: &Matrix3<f64>) -> Matrix3<f64> {
    let t00 = phi[(0, 0)] * p[(0, 0)] + phi[(0, 1)] * p[(1, 0)] + phi[(0, 2)] * p[(2, 0)];
    let t01 = phi[(0, 0)] * p[(0, 1)] + phi[(0, 1)] * p[(1, 1)] + phi[(0, 2)] * p[(2, 1)];
    let t02 = phi[(0, 0)] * p[(0, 2)] + phi[(0, 1)] * p[(1, 2)] + phi[(0, 2)] * p[(2, 2)];
    let t10 = phi[(1, 0)] * p[(0, 0)] + phi[(1, 1)] * p[(1, 0)] + phi[(1, 2)] * p[(2, 0)];
    let t11 = phi[(1, 0)] * p[(0, 1)] + phi[(1, 1)] * p[(1, 1)] + phi[(1, 2)] * p[(2, 1)];
    let t12 = phi[(1, 0)] * p[(0, 2)] + phi[(1, 1)] * p[(1, 2)] + phi[(1, 2)] * p[(2, 2)];
    let t20 = phi[(2, 0)] * p[(0, 0)] + phi[(2, 1)] * p[(1, 0)] + phi[(2, 2)] * p[(2, 0)];
    let t21 = phi[(2, 0)] * p[(0, 1)] + phi[(2, 1)] * p[(1, 1)] + phi[(2, 2)] * p[(2, 1)];
    let t22 = phi[(2, 0)] * p[(0, 2)] + phi[(2, 1)] * p[(1, 2)] + phi[(2, 2)] * p[(2, 2)];

    Matrix3::new(
        t00 * phi[(0, 0)] + t01 * phi[(0, 1)] + t02 * phi[(0, 2)] + q[(0, 0)],
        t00 * phi[(1, 0)] + t01 * phi[(1, 1)] + t02 * phi[(1, 2)] + q[(0, 1)],
        t00 * phi[(2, 0)] + t01 * phi[(2, 1)] + t02 * phi[(2, 2)] + q[(0, 2)],
        t10 * phi[(0, 0)] + t11 * phi[(0, 1)] + t12 * phi[(0, 2)] + q[(1, 0)],
        t10 * phi[(1, 0)] + t11 * phi[(1, 1)] + t12 * phi[(1, 2)] + q[(1, 1)],
        t10 * phi[(2, 0)] + t11 * phi[(2, 1)] + t12 * phi[(2, 2)] + q[(1, 2)],
        t20 * phi[(0, 0)] + t21 * phi[(0, 1)] + t22 * phi[(0, 2)] + q[(2, 0)],
        t20 * phi[(1, 0)] + t21 * phi[(1, 1)] + t22 * phi[(1, 2)] + q[(2, 1)],
        t20 * phi[(2, 0)] + t21 * phi[(2, 1)] + t22 * phi[(2, 2)] + q[(2, 2)],
    )
}

#[inline]
fn joseph_covariance(p: &Matrix3<f64>, k: [f64; 3], r: f64) -> Matrix3<f64> {
    let a00 = 1.0 - k[0];
    let t00 = a00 * p[(0, 0)];
    let t01 = a00 * p[(0, 1)];
    let t02 = a00 * p[(0, 2)];
    let t10 = p[(1, 0)] - k[1] * p[(0, 0)];
    let t11 = p[(1, 1)] - k[1] * p[(0, 1)];
    let t12 = p[(1, 2)] - k[1] * p[(0, 2)];
    let t20 = p[(2, 0)] - k[2] * p[(0, 0)];
    let t21 = p[(2, 1)] - k[2] * p[(0, 1)];
    let t22 = p[(2, 2)] - k[2] * p[(0, 2)];

    Matrix3::new(
        t00 * a00 + r * k[0] * k[0],
        t01 - t00 * k[1] + r * k[0] * k[1],
        t02 - t00 * k[2] + r * k[0] * k[2],
        t10 * a00 + r * k[1] * k[0],
        t11 - t10 * k[1] + r * k[1] * k[1],
        t12 - t10 * k[2] + r * k[1] * k[2],
        t20 * a00 + r * k[2] * k[0],
        t21 - t20 * k[1] + r * k[2] * k[1],
        t22 - t20 * k[2] + r * k[2] * k[2],
    )
}

fn symmetrize_psd(p: Matrix3<f64>) -> Matrix3<f64> {
    0.5 * (p + p.transpose())
}

#[cfg(test)]
mod tests {
    use super::*;
    use nalgebra::RowVector3;

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

    #[test]
    fn invalid_ticks_do_not_dilute_recent_criticality() {
        let init = ObserverInit {
            name: "invalid-ring".into(),
            theta: 1.0,
            q_process: None,
            r_measurement: Some(1e-8),
            ring_buffer_capacity: Some(100),
        };
        let mut o = BasisObserver::new(init).unwrap();
        for tick in 0..20 {
            o.update(tick as f64, 100.0, 100.0);
        }
        for tick in 20..40 {
            o.update(tick as f64, 130.0, 100.0);
        }
        let before = o.criticality_recent(crate::sentinel::CRITICALITY_WINDOW);
        assert!(
            before > crate::sentinel::CRITICALITY_AUDIT_PCT,
            "criticality={before}"
        );

        for tick in 40..240 {
            let health = o.update(tick as f64, 100.0, 0.0);
            assert!(!health.measurement_valid);
        }

        assert_eq!(
            o.criticality_recent(crate::sentinel::CRITICALITY_WINDOW),
            before
        );
    }

    #[test]
    fn running_criticality_matches_naive_scan_after_wraparound() {
        let mut o = BasisObserver::new(ObserverInit {
            name: "criticality-wrap".into(),
            theta: 1.0,
            q_process: None,
            r_measurement: Some(1e-8),
            ring_buffer_capacity: Some(32),
        })
        .unwrap();
        for tick in 0..300 {
            let token = if tick % 11 < 3 { 82.0 } else { 100.0 };
            o.update(tick as f64, 100.0, token);
            for window in [1, 5, 20, 32, 100] {
                let width = window.min(o.ring.len());
                let naive = if width == 0 {
                    0.0
                } else {
                    let breaches = (o.ring.len() - width..o.ring.len())
                        .filter(|index| o.ring_sample(*index).breach)
                        .count();
                    100.0 * breaches as f64 / width as f64
                };
                assert_eq!(o.criticality_recent(window), naive);
            }
            let naive_full = if o.ring.is_empty() {
                0.0
            } else {
                let breaches = (0..o.ring.len())
                    .filter(|index| o.ring_sample(*index).breach)
                    .count();
                100.0 * breaches as f64 / o.ring.len() as f64
            };
            assert_eq!(o.criticality_score(), naive_full);
        }
    }

    #[test]
    fn single_pass_window_stats_match_reference() {
        let mut o = BasisObserver::new(ObserverInit {
            name: "window-stats".into(),
            theta: 1.0,
            q_process: None,
            r_measurement: Some(1e-7),
            ring_buffer_capacity: Some(64),
        })
        .unwrap();
        for tick in 0..200 {
            let physical = 100.0 + ((tick * 17) % 29) as f64 * 0.002;
            let token = 100.0 + ((tick * 11) % 23) as f64 * 0.001;
            o.update(tick as f64, physical, token);
        }
        let values: Vec<f64> = o.ring_iter().map(|sample| sample.innovation).collect();
        let count = values.len() as f64;
        let mean = values.iter().sum::<f64>() / count;
        let variance = values
            .iter()
            .map(|value| (value - mean).powi(2))
            .sum::<f64>()
            / (count - 1.0);
        let sum_z4 = values
            .iter()
            .map(|value| ((value - mean) / variance.sqrt()).powi(4))
            .sum::<f64>();
        let kurtosis = (count * (count + 1.0) / ((count - 1.0) * (count - 2.0) * (count - 3.0)))
            * sum_z4
            - 3.0 * (count - 1.0).powi(2) / ((count - 2.0) * (count - 3.0));
        let actual = o.window_stats();
        assert!((actual.mean - mean).abs() < 1e-15);
        assert!((actual.variance - variance).abs() < 1e-18);
        assert!((actual.kurtosis - kurtosis).abs() < 1e-8);
    }

    #[test]
    fn specialized_kalman_matches_nalgebra_reference() {
        let mut optimized = BasisObserver::new(ObserverInit {
            name: "specialized-parity".into(),
            theta: 0.7,
            q_process: None,
            r_measurement: Some(1e-6),
            ring_buffer_capacity: Some(0),
        })
        .unwrap();
        let h = RowVector3::new(1.0, 0.0, 0.0);
        let q = optimized.q;
        let r = optimized.r;
        let mut reference_x = Vector3::zeros();
        let mut reference_p = Matrix3::identity() * 1e4;
        let mut reference_last_t: Option<f64> = None;
        let mut initialized = false;

        for tick in 0..5000 {
            let timestamp = if tick % 97 == 0 {
                tick as f64 * 0.001 + 0.000_000_3
            } else {
                tick as f64 * 0.001
            };
            let physical = 100.0 + ((tick % 31) as f64 - 15.0) * 1e-4;
            let token = 100.0 + ((tick % 19) as f64 - 9.0) * 7e-5;
            let z = (physical / token).ln();

            let expected = if !initialized {
                initialized = true;
                reference_last_t = Some(timestamp);
                reference_x = Vector3::new(z, 0.0, z);
                reference_p = Matrix3::from_diagonal(&Vector3::new(r * 10.0, r * 100.0, r * 10.0));
                SystemHealth {
                    filtered_basis: z,
                    innovation: 0.0,
                    mahalanobis_distance: 0.0,
                    measurement_valid: true,
                }
            } else {
                let dt_raw = timestamp - reference_last_t.unwrap();
                let dt = if dt_raw > 0.0 { dt_raw } else { 0.0 };
                reference_last_t = Some(timestamp);
                let phi = if dt > 0.0 {
                    phi_matrix(0.7, dt)
                } else {
                    Matrix3::identity()
                };
                let x_pred = phi * reference_x;
                let p_pred = phi * reference_p * phi.transpose() + q;
                let innovation = z - (h * x_pred)[0];
                let s = (h * p_pred * h.transpose())[0] + r;
                let k = p_pred * h.transpose() * (1.0 / s);
                let kh = Matrix3::new(k[0], 0.0, 0.0, k[1], 0.0, 0.0, k[2], 0.0, 0.0);
                let a = Matrix3::identity() - kh;
                reference_p = symmetrize_psd(a * p_pred * a.transpose() + k * k.transpose() * r);
                reference_x = x_pred + k * innovation;
                SystemHealth {
                    filtered_basis: reference_x[0],
                    innovation,
                    mahalanobis_distance: innovation.abs() / s.max(1e-300).sqrt(),
                    measurement_valid: true,
                }
            };

            let actual = optimized.update(timestamp, physical, token);
            assert!((actual.filtered_basis - expected.filtered_basis).abs() < 1e-12);
            assert!((actual.innovation - expected.innovation).abs() < 1e-12);
            assert!((actual.mahalanobis_distance - expected.mahalanobis_distance).abs() < 1e-12);
            for row in 0..3 {
                assert!((optimized.x[row] - reference_x[row]).abs() < 1e-12);
                for column in 0..3 {
                    assert!(
                        (optimized.p[(row, column)] - reference_p[(row, column)]).abs() < 1e-12
                    );
                }
            }
        }
    }
}
