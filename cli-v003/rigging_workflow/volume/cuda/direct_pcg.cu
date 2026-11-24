// Standalone batched Jacobi-PCG probe. Each column is an independent system.
// Block dimensions for multicolumn reductions must be (32, 8, 1).

enum State { DONE, ACTIVE, VERIFY, FAILED, RESTART };

template <typename T>
__device__ T csr_value(const int* offsets, const int* columns, const T* values,
                       const T* vector, int row, int bone, int pitch)
{
    T value = 0;
    for (int index = offsets[row]; index < offsets[row + 1]; ++index) {
        value += values[index] * vector[columns[index] * pitch + bone];
    }
    return value;
}

extern "C" __global__ void csr_float(const int* offsets, const int* columns,
                                      const float* values, const float* direction,
                                      float* product, const int* state, int n, int pitch)
{
    int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= n * pitch) return;
    int bone = index % pitch;
    product[index] = state[bone] == ACTIVE
        ? csr_value(offsets, columns, values, direction, index / pitch, bone, pitch)
        : 0.0f;
}

// Rows remain coalesced by bone, and each block contributes once per bone.
extern "C" __global__ void reduce_float(const float* x, const float* other,
                                         const float* inverse_degree, const int* state,
                                         float* sum_product, float* sum_square,
                                         int n, int pitch, int precondition)
{
    __shared__ float product[8][32];
    __shared__ float square[8][32];
    int lane = threadIdx.x;
    int bone = blockIdx.y * 32 + lane;
    bool active = state[bone] == ACTIVE;
    int y = threadIdx.y;
    int begin = blockIdx.x * 256;
    int end = min(begin + 256, n);
    float local_product = 0.0f, local_square = 0.0f;
    if (active) {
        for (int row = begin + y; row < end; row += 8) {
            int index = row * pitch + bone;
            float value = x[index];
            if (precondition) {
                float squared = value * value;
                local_square += squared;
                local_product += squared * inverse_degree[row];
            }
            else local_product += value * other[index];
        }
    }
    product[y][lane] = local_product;
    square[y][lane] = local_square;
    // Some bones can be inactive, so all threads must reach the barrier.
    __syncthreads();
    if (active && y == 0) {
        for (int i = 1; i < 8; ++i) {
            local_product += product[i][lane];
            local_square += square[i][lane];
        }
        atomicAdd(sum_product + bone, local_product);
        if (precondition) atomicAdd(sum_square + bone, local_square);
    }
}

extern "C" __global__ void choose_step(int* state, int* iterations, const float* rho,
                                       const float* curvature, float* step)
{
    int bone = threadIdx.x;
    step[bone] = 0.0f;
    if (state[bone] != ACTIVE) return;
    ++iterations[bone];
    float ratio = rho[bone] / curvature[bone];
    if (rho[bone] <= 0.0f || curvature[bone] <= 0.0f || !isfinite(ratio)) {
        state[bone] = VERIFY;
    }
    else step[bone] = ratio;
}

extern "C" __global__ void advance_solution(float* solution, float* residual,
                                            const float* direction, const float* product,
                                            const int* state, const float* step,
                                            int n, int pitch)
{
    int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= n * pitch) return;
    int bone = index % pitch;
    if (state[bone] == ACTIVE) {
        solution[index] += step[bone] * direction[index];
        residual[index] -= step[bone] * product[index];
    }
}

extern "C" __global__ void choose_direction(int* state, float* rho, const float* next_rho,
                                            const float* norm2, const double* target,
                                            const int* iterations, float* beta, int max_iterations)
{
    int bone = threadIdx.x;
    beta[bone] = 0.0f;
    if (state[bone] != ACTIVE) return;
    if (norm2[bone] <= target[bone] || !isfinite(norm2[bone]) || next_rho[bone] <= 0.0f
        || iterations[bone] >= max_iterations) {
        state[bone] = VERIFY;
    }
    else {
        beta[bone] = next_rho[bone] / rho[bone];
        rho[bone] = next_rho[bone];
    }
}

extern "C" __global__ void advance_direction(float* direction, const float* residual,
                                             const float* inverse_degree, const int* state,
                                             const float* beta, int n, int pitch)
{
    int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= n * pitch) return;
    int bone = index % pitch;
    if (state[bone] == ACTIVE) {
        direction[index] = residual[index] * inverse_degree[index / pitch]
                         + beta[bone] * direction[index];
    }
}

extern "C" __global__ void commit_correction(double* solution, float* correction,
                                             const int* state, int n, int pitch)
{
    int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= n * pitch) return;
    if (state[index % pitch] == VERIFY) {
        solution[index] += correction[index];
        correction[index] = 0.0f;
    }
}

extern "C" __global__ void csr_true_residual(const int* offsets, const int* columns,
                                             const double* values, const double* solution,
                                             const double* rhs, double* residual,
                                             const int* state, int n, int pitch)
{
    int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= n * pitch) return;
    int bone = index % pitch;
    if (state[bone] == VERIFY) {
        residual[index] = rhs[index]
            - csr_value(offsets, columns, values, solution, index / pitch, bone, pitch);
    }
}

extern "C" __global__ void reduce_true_residual(const double* residual,
                                                const float* inverse_degree, const int* state,
                                                double* out_norm, double* out_rho, int n, int pitch)
{
    __shared__ double norms[8][32];
    __shared__ double rhos[8][32];
    int lane = threadIdx.x;
    int bone = blockIdx.y * 32 + lane;
    bool verifying = state[bone] == VERIFY;
    int y = threadIdx.y;
    int begin = blockIdx.x * 256;
    int end = min(begin + 256, n);
    double local_norm = 0.0, local_rho = 0.0;
    if (verifying) {
        for (int row = begin + y; row < end; row += 8) {
            double value = residual[row * pitch + bone];
            double squared = value * value;
            local_norm += squared;
            local_rho += squared * inverse_degree[row];
        }
    }
    norms[y][lane] = local_norm;
    rhos[y][lane] = local_rho;
    __syncthreads();
    if (verifying && y == 0) {
        for (int i = 1; i < 8; ++i) {
            local_norm += norms[i][lane];
            local_rho += rhos[i][lane];
        }
        atomicAdd(out_norm + bone, local_norm);
        atomicAdd(out_rho + bone, local_rho);
    }
}

extern "C" __global__ void finish_verification(int* state, const int* iterations,
                                                int* restarts, float* rho, double* target,
                                                double* relative, const double* original_norm2,
                                                const double* norm2, const double* next_rho,
                                                int max_iterations, double tolerance2,
                                                double inner_factor2)
{
    int bone = threadIdx.x;
    if (state[bone] != VERIFY) return;
    relative[bone] = sqrt(norm2[bone] / original_norm2[bone]);
    if (!isfinite(relative[bone])) {
        state[bone] = FAILED;
    }
    else if (norm2[bone] <= tolerance2 * original_norm2[bone]) {
        state[bone] = DONE;
    }
    else if (iterations[bone] >= max_iterations || next_rho[bone] <= 0.0) {
        state[bone] = FAILED;
    }
    else {
        state[bone] = RESTART;
        ++restarts[bone];
        rho[bone] = static_cast<float>(next_rho[bone]);
        target[bone] = inner_factor2 * norm2[bone];
    }
}

extern "C" __global__ void restart_vectors(float* residual, float* direction,
                                          const double* true_residual, const float* inverse_degree,
                                          const int* state, int n, int pitch)
{
    int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= n * pitch) return;
    if (state[index % pitch] == RESTART) {
        float value = static_cast<float>(true_residual[index]);
        residual[index] = value;
        direction[index] = value * inverse_degree[index / pitch];
    }
}

extern "C" __global__ void resume_bones(int* state)
{
    int bone = threadIdx.x;
    if (state[bone] == RESTART) state[bone] = ACTIVE;
}
