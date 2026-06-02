import { createSlice, type PayloadAction } from '@reduxjs/toolkit';

/** Holds the current id token so the RTK Query base query can authorize calls. */
export interface AuthSliceState {
  idToken: string | null;
}

const initialState: AuthSliceState = { idToken: null };

const authSlice = createSlice({
  name: 'auth',
  initialState,
  reducers: {
    setIdToken(state, action: PayloadAction<string | null>) {
      state.idToken = action.payload;
    },
  },
});

export const { setIdToken } = authSlice.actions;
export const authReducer = authSlice.reducer;
