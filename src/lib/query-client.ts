import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'
import { handleError } from './utils'
import { shouldRetry } from './errors'

export const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => { if (!query.meta?.silentError) handleError(error) },
  }),
  mutationCache: new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      // Mutations with local validation feedback own their error presentation.
      if (!mutation.options.onError) handleError(error)
    },
  }),
  defaultOptions: {
    queries: { staleTime: 30_000, retry: shouldRetry },
    mutations: { retry: false },
  },
})
