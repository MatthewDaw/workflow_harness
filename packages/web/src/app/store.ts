import { configureStore } from '@reduxjs/toolkit';
import { setupListeners } from '@reduxjs/toolkit/query';
import { baseApi } from '../api/baseApi.js';
import { learningApi } from '../api/learningApi.js';
import { authReducer } from './authSlice.js';
import { liveEventsReducer } from './liveEventsSlice.js';
import { liveMiddleware } from '../ws/liveMiddleware.js';

/** Build the Redux store (a factory so tests get an isolated instance). */
export function makeStore() {
  const store = configureStore({
    reducer: {
      [baseApi.reducerPath]: baseApi.reducer,
      [learningApi.reducerPath]: learningApi.reducer,
      auth: authReducer,
      liveEvents: liveEventsReducer,
    },
    middleware: (getDefault) =>
      getDefault().concat(baseApi.middleware, learningApi.middleware, liveMiddleware),
  });
  setupListeners(store.dispatch);
  return store;
}

export type AppStore = ReturnType<typeof makeStore>;
export type RootState = ReturnType<AppStore['getState']>;
export type AppDispatch = AppStore['dispatch'];
